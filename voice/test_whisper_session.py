"""Pi voice sequencing tests without microphone, speaker or Whisper."""

import array
import io
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from voice.stt import whisper_session as session


class WhisperSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.paths = [self.root / name for name in ("observe.mp3", "whisper-cli", "ggml-base-q8_0.bin")]
        for path in self.paths:
            path.touch()
        self.calls = []
        self.samples = [500, -500] * 100

    def command(self, args, timeout):
        self.calls.append(args)
        if args[0] == "arecord":
            with wave.open(args[-1], "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                samples = array.array("h", self.samples)
                if sys.byteorder != "little":
                    samples.byteswap()
                audio.writeframes(samples.tobytes())
        return subprocess.CompletedProcess(args, 0, stdout="도와주세요.\n", stderr="")

    def exchange(self, **options):
        with patch.object(session.shutil, "which", return_value="/mock/bin"), \
             patch.object(session, "run_command", side_effect=self.command):
            return session.exchange(prompt=self.paths[0], cli=self.paths[1], model=self.paths[2], **options)

    def test_playback_finishes_before_recording_and_inference(self):
        result = self.exchange(input_device="plughw:TEST", output_device="plughw:SPEAKER")
        self.assertEqual([Path(c[0]).name for c in self.calls], ["mpg123", "arecord", "whisper-cli"])
        self.assertEqual(result["classification"], "도움 필요")
        self.assertFalse(result["speech_validated"])
        self.assertIn(str(self.paths[2]), self.calls[-1])
        self.assertIn("512", self.calls[-1])
        self.assertFalse(Path(self.calls[1][-1]).exists())

    def test_playback_failure_never_opens_microphone(self):
        def fail(args, timeout):
            self.calls.append(args)
            raise subprocess.CalledProcessError(1, args)
        self.command = fail
        result = self.exchange()
        self.assertEqual(result["stage"], "playback")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["classification"], "확인 필요")

    def test_recording_failure_never_starts_whisper(self):
        original = self.command
        def fail(args, timeout):
            if args[0] == "arecord":
                raise RuntimeError("microphone disconnected")
            return original(args, timeout)
        self.command = fail
        result = self.exchange()
        self.assertEqual(result["stage"], "recording")
        self.assertEqual(len(self.calls), 1)

    def test_inference_timeout_remains_error_and_removes_recording(self):
        original = self.command
        def fail(args, timeout):
            if args[0] == str(self.paths[1]):
                raise subprocess.TimeoutExpired(args, timeout)
            return original(args, timeout)
        self.command = fail
        result = self.exchange()
        self.assertEqual((result["status"], result["stage"]), ("error", "recognition"))
        self.assertEqual(result["classification"], "확인 필요")
        self.assertFalse(Path(self.calls[1][-1]).exists())

    def test_digital_silence_skips_whisper_and_never_means_refusal(self):
        self.samples = [0] * 320
        result = self.exchange()
        self.assertEqual(result["input_condition"], "silence")
        self.assertEqual(result["classification"], "확인 필요")
        self.assertEqual(len(self.calls), 2)

    def test_missing_model_stops_before_playback(self):
        self.paths[2].unlink()
        result = self.exchange()
        self.assertEqual(result["stage"], "preparation")
        self.assertEqual(self.calls, [])

    def test_cli_defaults_to_selected_quantized_model(self):
        with patch.object(sys, "argv", ["whisper_session.py"]), \
             patch.object(session, "exchange", return_value={"status": "ok"}) as exchange, \
             patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(session.main(), 0)
        self.assertEqual(exchange.call_args.kwargs["model"].name, "ggml-base-q8_0.bin")

    def test_sigterm_during_recording_removes_temporary_wav(self):
        marker = self.root / 'recording-path.txt'
        # Real Python signal handling with fake audio commands, no devices.
        code = '''
import sys,time
from pathlib import Path
from voice.stt import whisper_session as session
marker=Path(sys.argv[1])
session.shutil.which=lambda name: '/mock/bin'
def command(args, timeout):
    if args[0] == 'arecord':
        Path(args[-1]).write_bytes(b'temporary recording')
        marker.write_text(args[-1])
        time.sleep(30)
    return type('Result', (), {'stdout': ''})()
session.run_command=command
sys.argv=['session','--prompt',sys.argv[2],'--whisper-cli',sys.argv[3],'--model',sys.argv[4]]
session.main()
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(marker)]
                                   + [str(path) for path in self.paths], start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            audio = Path(marker.read_text())
            os.killpg(process.pid, signal.SIGTERM)
            self.assertEqual(process.wait(timeout=3), 130)
            self.assertFalse(audio.exists())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    unittest.main()
