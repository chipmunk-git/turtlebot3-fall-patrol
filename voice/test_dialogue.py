"""Exercise the real dialogue/STT flow with mocked audio, model and player."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from voice import dialogue
from voice.stt import record_korean as stt


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.text = "  도와 주세요!  "
        self.samples = object()
        self.audio = Mock()
        self.audio.reshape.return_value = self.samples
        self.sd = types.ModuleType("sounddevice")
        self.sd.check_input_settings = Mock(side_effect=lambda **kw: self.events.append("input"))
        self.sd.query_devices = Mock(return_value={"name": "Mock microphone"})
        self.sd.rec = Mock(side_effect=self.record)
        self.sd.wait = Mock(side_effect=lambda **kw: self.events.append("recorded"))
        self.sd.stop = Mock()
        self.model = Mock()
        self.model.transcribe = Mock(side_effect=self.transcribe)
        self.whisper = types.ModuleType("whisper")
        self.whisper.load_model = Mock(side_effect=self.load_model)
        self.play = Mock(side_effect=self.play_prompt)

    def load_model(self, *args, **kwargs):
        self.events.append("model")
        return self.model

    def play_prompt(self, command, **kwargs):
        self.events.append("playback")
        self.sd.rec.assert_not_called()
        return subprocess.CompletedProcess(command, 0)

    def record(self, *args, **kwargs):
        self.events.append("recording")
        return self.audio

    def transcribe(self, *args, **kwargs):
        self.events.append("recognition")
        return {"text": self.text}

    @contextlib.contextmanager
    def environment(self, platform="linux"):
        modules = {"sounddevice": self.sd, "whisper": self.whisper}
        clock = [0.0, 1.5, 1.5, 6.5, 6.5, 8.0, 8.0, 13.0, 13.0, 14.5]
        with patch.dict(sys.modules, modules), patch.object(sys, "platform", platform):
            with patch.object(dialogue.subprocess, "run", self.play):
                with patch.object(stt.time, "perf_counter", side_effect=clock):
                    yield

    def test_model_and_playback_finish_before_recording_and_result_is_returned(self):
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(
            self.events, ["input", "model", "playback", "recording", "recorded", "recognition"]
        )
        self.assertEqual(result, {
            "status": "ok",
            "text": "도와 주세요!",
            "classification": "도움 필요",
            "record_seconds": 5.0,
            "transcribe_seconds": 1.5,
            "model_seconds": 1.5,
        })
        self.play.assert_called_once_with(
            ["mpg123", "-q", str(dialogue.DEFAULT_PROMPT)],
            check=True, capture_output=True, text=True,
        )
        self.sd.rec.assert_called_once_with(
            80000, samplerate=16000, channels=1, dtype="float32", device=None
        )
        self.sd.wait.assert_called_once_with(ignore_errors=False)
        self.model.transcribe.assert_called_once_with(
            self.samples, language="ko", task="transcribe", fp16=False, verbose=None
        )

    def test_mac_uses_afplay(self):
        with self.environment(platform="darwin"):
            result = dialogue.ask_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.play.call_args.args[0], ["afplay", str(dialogue.DEFAULT_PROMPT)])

    def test_player_can_be_selected_explicitly(self):
        with self.environment(platform="darwin"):
            result = dialogue.ask_once(player="mpg123")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.play.call_args.args[0][0], "mpg123")

    def test_prompt_path_with_spaces_is_passed_as_one_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "test prompt.mp3"
            prompt.write_bytes(b"mock audio")
            with self.environment():
                result = dialogue.ask_once(prompt)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.play.call_args.args[0], ["mpg123", "-q", str(prompt.resolve())])
        self.assertNotIn("shell", self.play.call_args.kwargs)

    def test_default_prompt_does_not_depend_on_working_directory(self):
        with self.environment(), patch("os.getcwd", return_value="/private/tmp"):
            result = dialogue.ask_once()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(Path(self.play.call_args.args[0][-1]).is_absolute())
        self.assertEqual(self.play.call_args.args[0][-1], str(dialogue.DEFAULT_PROMPT))

    def test_missing_prompt_stops_before_model_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.environment():
                result = dialogue.ask_once(Path(directory) / "missing.mp3")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stage"], "playback")
        self.whisper.load_model.assert_not_called()
        self.play.assert_not_called()
        self.sd.rec.assert_not_called()

    def test_unsupported_player_stops_before_model_preparation(self):
        with self.environment():
            result = dialogue.ask_once(player="unsupported")
        self.assertEqual(result["stage"], "playback")
        self.whisper.load_model.assert_not_called()
        self.sd.rec.assert_not_called()

    def test_missing_player_returns_failure_without_recording(self):
        self.play.side_effect = FileNotFoundError("mpg123 not installed")
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(result, {
            "status": "error", "stage": "playback", "message": "mpg123 not installed"
        })
        self.sd.rec.assert_not_called()

    def test_failed_playback_returns_player_error_without_recording(self):
        self.play.side_effect = subprocess.CalledProcessError(
            1, ["mpg123"], stderr="audio output unavailable\n"
        )
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(result, {
            "status": "error", "stage": "playback", "message": "audio output unavailable"
        })
        self.sd.rec.assert_not_called()
        self.model.transcribe.assert_not_called()

    def test_invalid_input_stops_before_model_preparation_and_playback(self):
        self.sd.check_input_settings.side_effect = RuntimeError("invalid input")
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stage"], "preparation")
        self.whisper.load_model.assert_not_called()
        self.play.assert_not_called()

    def test_model_failure_stops_before_playback_and_recording(self):
        self.whisper.load_model.side_effect = RuntimeError("model unavailable")
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(result, {
            "status": "error", "stage": "preparation", "message": "model unavailable"
        })
        self.play.assert_not_called()
        self.sd.rec.assert_not_called()

    def test_recording_failure_stops_stream_and_does_not_return_a_classification(self):
        self.sd.wait.side_effect = RuntimeError("microphone disconnected")
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(result, {
            "status": "error", "stage": "recording", "message": "microphone disconnected"
        })
        self.sd.stop.assert_called_once_with()
        self.model.transcribe.assert_not_called()
        self.assertNotIn("classification", result)

    def test_audio_overflow_stops_before_transcription(self):
        self.sd.wait.return_value = "input overflow"
        self.sd.wait.side_effect = None
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stage"], "recording")
        self.assertIn("input overflow", result["message"])
        self.sd.stop.assert_called_once_with()
        self.model.transcribe.assert_not_called()

    def test_transcription_failure_is_distinct_from_confirmation_required(self):
        self.model.transcribe.side_effect = RuntimeError("inference failed")
        with self.environment():
            result = dialogue.ask_once()
        self.assertEqual(result, {
            "status": "error", "stage": "recognition", "message": "inference failed"
        })
        self.assertNotIn("classification", result)

    def test_empty_or_unregistered_text_requires_confirmation(self):
        for text in ("", "괜찮아요. 그런데 못 일어나겠어요."):
            with self.subTest(text=text), self.environment():
                self.text = text
                self.sd.rec.reset_mock()
                result = dialogue.ask_once()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["text"], text)
            self.assertEqual(result["classification"], "확인 필요")

    def test_prepared_model_is_reused_for_multiple_responses(self):
        self.play.side_effect = lambda *a, **kw: subprocess.CompletedProcess(a[0], 0)
        with self.environment():
            recognizer = stt.KoreanRecognizer(device=3)
            first = dialogue.ask_once(recognizer=recognizer)
            second = dialogue.ask_once(recognizer=recognizer)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.whisper.load_model.assert_called_once_with(
            "base", device="cpu", download_root=str(stt.MODEL_DIR)
        )
        self.assertEqual(self.play.call_count, 2)
        self.assertEqual(self.sd.rec.call_count, 2)
        self.assertTrue(all(call.kwargs["device"] == 3 for call in self.sd.rec.call_args_list))

    def test_device_selection_is_applied_before_playback(self):
        with self.environment():
            result = dialogue.ask_once(device=3)
        self.assertEqual(result["status"], "ok")
        self.sd.check_input_settings.assert_called_once_with(
            device=3, samplerate=16000, channels=1, dtype="float32"
        )
        self.assertEqual(self.sd.rec.call_args.kwargs["device"], 3)

    def test_prepared_recognizer_does_not_silently_ignore_a_device_argument(self):
        with self.environment():
            recognizer = stt.KoreanRecognizer(device=3)
            result = dialogue.ask_once(recognizer=recognizer, device=4)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stage"], "preparation")
        self.play.assert_not_called()
        self.sd.rec.assert_not_called()

    def test_recording_interrupt_stops_stream_and_propagates(self):
        self.sd.wait.side_effect = KeyboardInterrupt
        with self.environment():
            with self.assertRaises(KeyboardInterrupt):
                dialogue.ask_once()
        self.sd.stop.assert_called_once_with()
        self.model.transcribe.assert_not_called()

    def test_programmatic_call_does_not_print_the_result(self):
        output = io.StringIO()
        with self.environment(), contextlib.redirect_stdout(output):
            result = dialogue.ask_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(output.getvalue(), "")


class DialogueCliTests(unittest.TestCase):
    def run_cli(self, result=None, error=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["voice.dialogue", "--device", "3"]):
            with patch.object(dialogue, "ask_once", return_value=result, side_effect=error):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = dialogue.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_outputs_json_with_success_exit_code(self):
        result = {"status": "ok", "text": "도와주세요", "classification": "도움 필요"}
        code, output, error = self.run_cli(result=result)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), result)
        self.assertEqual(error, "")

    def test_cli_outputs_failure_as_json_with_nonzero_exit_code(self):
        result = {"status": "error", "stage": "playback", "message": "player not found"}
        code, output, error = self.run_cli(result=result)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), result)
        self.assertEqual(error, "")

    def test_cli_interrupt_has_exit_code_130(self):
        code, output, error = self.run_cli(error=KeyboardInterrupt)
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        self.assertIn("중단했습니다", error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
