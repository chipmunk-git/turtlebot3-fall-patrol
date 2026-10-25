"""Test the recording/transcription flow without a microphone or model download."""

import contextlib
import io
import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).with_name("record_korean.py")


class RecordKoreanTests(unittest.TestCase):
    def setUp(self):
        self.samples = object()
        self.audio = Mock()
        self.audio.reshape.return_value = self.samples
        self.sd = types.ModuleType("sounddevice")
        self.sd.check_input_settings = Mock()
        self.sd.query_devices = Mock(return_value={"name": "Mock Mac microphone"})
        self.sd.rec = Mock(return_value=self.audio)
        self.sd.wait = Mock(return_value=None)
        self.sd.stop = Mock()
        self.model = Mock()
        self.model.transcribe.return_value = {"text": "  안녕하세요.  "}
        self.whisper = types.ModuleType("whisper")
        self.whisper.load_model = Mock(return_value=self.model)

    def run_program(self, args=(), missing=()):
        modules = {"sounddevice": self.sd, "whisper": self.whisper}
        modules.update({name: None for name in missing})
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]):
            namespace = runpy.run_path(str(SCRIPT), run_name="stt_test_target")
        clock = Mock(side_effect=[0.0, 1.5, 1.5, 6.5, 6.5, 8.0])
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", [str(SCRIPT), *args]):
            with patch.dict(namespace["main"].__globals__, time=types.SimpleNamespace(perf_counter=clock)):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = namespace["main"]()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_five_seconds_of_mono_audio_are_transcribed_in_korean(self):
        code, output, error = self.run_program()
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.sd.rec.assert_called_once_with(
            80000, samplerate=16000, channels=1, dtype="float32", device=None
        )
        self.sd.wait.assert_called_once_with(ignore_errors=False)
        self.audio.reshape.assert_called_once_with(-1)
        self.model.transcribe.assert_called_once_with(
            self.samples, language="ko", task="transcribe", fp16=False, verbose=None
        )
        self.whisper.load_model.assert_called_once_with(
            "base", device="cpu", download_root=str(SCRIPT.parent / ".models")
        )
        self.assertIn("인식 문장: 안녕하세요.", output)
        self.assertIn("응답 분류: 확인 필요", output)
        self.assertIn("처리 시간(음성 인식): 1.50초", output)
        self.assertIn("녹음 소요 시간: 5.00초", output)
        self.assertIn("모델 준비 시간: 1.50초", output)

    def test_device_selection_is_applied_to_input_and_recording(self):
        code, _, _ = self.run_program(["--device", "3"])
        self.assertEqual(code, 0)
        self.sd.check_input_settings.assert_called_once_with(
            device=3, samplerate=16000, channels=1, dtype="float32"
        )
        self.sd.query_devices.assert_called_once_with(3, "input")
        self.assertEqual(self.sd.rec.call_args.kwargs["device"], 3)

    def test_listing_devices_does_not_require_whisper_or_record(self):
        self.sd.query_devices.return_value = "0 Mock Mac microphone"
        code, output, error = self.run_program(["--list-devices"], missing=["whisper"])
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("0 Mock Mac microphone", output)
        self.sd.rec.assert_not_called()
        self.whisper.load_model.assert_not_called()

    def test_empty_transcription_is_displayed_explicitly(self):
        self.model.transcribe.return_value = {"text": "  "}
        code, output, _ = self.run_program()
        self.assertEqual(code, 0)
        self.assertIn("(인식된 문장 없음)", output)
        self.assertIn("응답 분류: 확인 필요", output)

    def test_registered_stt_results_print_their_classification(self):
        for text, expected in (
            ("도와 주세요!", "도움 필요"),
            ("사람 좀 불러주세요.", "도움 필요"),
            ("괜찮아요.", "도움 불필요"),
            ("도움이 필요 없어요.", "도움 불필요"),
        ):
            with self.subTest(text=text):
                self.model.transcribe.return_value = {"text": text}
                code, output, error = self.run_program()
                self.assertEqual(code, 0)
                self.assertEqual(error, "")
                self.assertIn(f"인식 문장: {text}", output)
                self.assertIn(f"응답 분류: {expected}", output)

    def test_ambiguous_stt_results_print_confirmation_required(self):
        for text in (
            "도와달라는 뜻은 아니에요.",
            "괜찮아요. 그런데 못 일어나겠어요.",
            "도움이 필요 없는 건 아니에요.",
        ):
            with self.subTest(text=text):
                self.model.transcribe.return_value = {"text": text}
                code, output, error = self.run_program()
                self.assertEqual(code, 0)
                self.assertEqual(error, "")
                self.assertIn("응답 분류: 확인 필요", output)

    def test_invalid_input_stops_before_model_download(self):
        self.sd.check_input_settings.side_effect = RuntimeError("no input device")
        code, _, error = self.run_program()
        self.assertEqual(code, 1)
        self.assertIn("no input device", error)
        self.whisper.load_model.assert_not_called()
        self.sd.rec.assert_not_called()

    def test_audio_overflow_stops_before_transcription(self):
        self.sd.wait.return_value = "input overflow"
        code, output, error = self.run_program()
        self.assertEqual(code, 1)
        self.assertIn("input overflow", error)
        self.assertNotIn("인식 문장:", output)
        self.assertNotIn("응답 분류:", output)
        self.model.transcribe.assert_not_called()

    def test_missing_dependency_has_a_clear_error(self):
        code, _, error = self.run_program(missing=["sounddevice"])
        self.assertEqual(code, 1)
        self.assertIn("의존성 로딩 실패", error)
        self.assertIn("backend/.venv", error)
        self.assertIn("voice/requirements.txt", error)
        self.model.transcribe.assert_not_called()

    def test_recording_interrupt_stops_the_audio_stream(self):
        self.sd.wait.side_effect = KeyboardInterrupt
        code, output, error = self.run_program()
        self.assertEqual(code, 130)
        self.sd.stop.assert_called_once_with()
        self.model.transcribe.assert_not_called()
        self.assertNotIn("인식 문장:", output)
        self.assertNotIn("응답 분류:", output)
        self.assertIn("중단했습니다", error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
