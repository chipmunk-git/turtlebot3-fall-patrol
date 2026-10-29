"""One Pi voice exchange; JSON output only, no server writes or saved recordings."""

import argparse
import array
import json
import math
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import wave

if __package__:
    from .response_classifier import classify_response, NEEDS_CONFIRMATION
else:
    from response_classifier import classify_response, NEEDS_CONFIRMATION

ROOT = Path(__file__).resolve().parents[2]


def run_command(command, timeout):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def audio_rms(path):
    with wave.open(str(path), "rb") as stream:
        if (stream.getnchannels(), stream.getframerate(), stream.getsampwidth()) != (1, 16000, 2):
            raise ValueError("녹음은 16 kHz, mono, PCM 16-bit WAV여야 합니다.")
        samples = array.array("h", stream.readframes(stream.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise ValueError("녹음 데이터가 없습니다.")
    return math.sqrt(sum(int(value) ** 2 for value in samples) / len(samples))


def exchange(*, prompt, cli, model, input_device="default", output_device="",
             inference_timeout=40.0, silence_rms=0.0):
    stage = "preparation"
    try:
        prompt, cli, model = (Path(p).expanduser().resolve() for p in (prompt, cli, model))
        for path in (prompt, cli, model):
            if not path.is_file():
                raise FileNotFoundError(str(path))
        for command in ("mpg123", "arecord"):
            if shutil.which(command) is None:
                raise FileNotFoundError(command)
        if (not math.isfinite(inference_timeout) or inference_timeout <= 0
                or not math.isfinite(silence_rms) or silence_rms < 0):
            raise ValueError("timeout must be positive; silence_rms must be nonnegative")

        stage = "playback"
        command = ["mpg123", "-q"]
        if output_device:
            command += ["-o", "alsa", "-a", output_device]
        run_command(command + [str(prompt)], 20.0)

        # Playback has exited successfully before the microphone is opened.
        with tempfile.TemporaryDirectory(prefix="fall_voice_") as directory:
            audio = Path(directory) / "speech.wav"
            stage = "recording"
            run_command(["arecord", "-q", "-D", input_device, "-f", "S16_LE",
                         "-r", "16000", "-c", "1", "-d", "5", "-t", "wav", str(audio)], 10.0)
            rms = audio_rms(audio)
            stage = "recognition"
            started = time.perf_counter()
            # Only digital silence is rejected by default. A calibrated energy
            # threshold can be supplied, but this is not a speech detector.
            if rms <= silence_rms:
                text, condition = "", "silence"
            else:
                result = run_command([str(cli), "-m", str(model), "-f", str(audio),
                                      "-l", "ko", "-t", "2", "-ac", "512", "-bs", "5",
                                      "-nt", "-np"], inference_timeout)
                text, condition = result.stdout.strip(), "speech_unverified"
            return {"status": "ok", "text": text[:2000],
                    "classification": classify_response(text), "input_condition": condition,
                    "speech_validated": False, "audio_rms": round(rms, 2),
                    "transcribe_seconds": round(time.perf_counter() - started, 3)}
    except Exception as error:
        return {"status": "error", "stage": stage, "message": str(error)[:1000],
                "classification": NEEDS_CONFIRMATION}


def main():
    parser = argparse.ArgumentParser(description="Pi 안내 재생 → 5초 녹음 → whisper.cpp → JSON")
    parser.add_argument("--prompt", type=Path, default=ROOT / "voice/tts/observe.mp3")
    parser.add_argument("--whisper-cli", type=Path,
                        default=Path.home() / "whisper.cpp/build/bin/whisper-cli")
    parser.add_argument("--model", type=Path,
                        default=Path.home() / "whisper.cpp/models/ggml-base-q8_0.bin")
    parser.add_argument("--input-device", default="default")
    parser.add_argument("--output-device", default="")
    parser.add_argument("--inference-timeout", type=float, default=40.0)
    parser.add_argument("--silence-rms", type=float, default=0.0)
    args = parser.parse_args()
    def terminate(signum, frame):
        # The ROS adapter cancels the entire process group. Unwind the WAV
        # temporary directory as well instead of leaving a recording behind.
        raise SystemExit(130)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        result = exchange(prompt=args.prompt, cli=args.whisper_cli, model=args.model,
                          input_device=args.input_device, output_device=args.output_device,
                          inference_timeout=args.inference_timeout, silence_rms=args.silence_rms)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
