"""Pi playback/recording, local or server inference, and JSON results."""

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
import uuid
import wave
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

if __package__:
    from .response_classifier import classify_response, NEEDS_CONFIRMATION
else:
    from response_classifier import classify_response, NEEDS_CONFIRMATION

ROOT = Path(__file__).resolve().parents[2]


def remote_endpoint(server_url):
    parsed = urlsplit(server_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("server-url은 http(s) 서버 기본 주소여야 합니다.")
    return server_url.rstrip("/") + "/voice/transcribe"


def run_command(command, timeout):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def report_progress(endpoint, request_id, expires_at, phase):
    """Best effort progress reporting must not prevent playback or cleanup."""
    if not endpoint or not isinstance(request_id, str) or expires_at is None:
        return
    if not math.isfinite(expires_at) or expires_at <= time.time():
        return
    url = endpoint.rsplit("/", 1)[0] + "/" + quote(request_id, safe="") + "/progress"
    body = json.dumps(dict(phase=phase, expires_at=expires_at)).encode()
    try:
        with urlopen(Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST"),
                     timeout=0.5):
            pass
    except (URLError, OSError, ValueError):
        pass


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


def recognize_file(audio, *, cli, model, inference_timeout=40.0, silence_rms=0.0):
    """Shared inference only: no playback, microphone, ROS2 or event writes."""
    cli, model = (Path(p).expanduser().resolve() for p in (cli, model))
    for path in (cli, model):
        if not path.is_file():
            raise FileNotFoundError(str(path))
    if (not math.isfinite(inference_timeout) or inference_timeout <= 0
            or not math.isfinite(silence_rms) or silence_rms < 0):
        raise ValueError("invalid inference parameters")
    rms = audio_rms(audio)
    started = time.perf_counter()
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


def upload_audio(audio, *, endpoint, request_id, expires_at, inference_timeout, silence_rms):
    """Retry the same recording/identity once; never repeat playback or recording."""
    boundary = "voice-" + uuid.uuid4().hex
    body = bytearray()
    for name, value in dict(request_id=request_id, expires_at=expires_at,
                            inference_timeout=inference_timeout, silence_rms=silence_rms).items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    body.extend(Path(audio).read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    for attempt in range(2):
        remaining = expires_at - time.time()
        if remaining <= 0:
            raise TimeoutError("음성 요청이 만료되었습니다.")
        request = Request(endpoint, data=bytes(body),
                          headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
        try:
            with urlopen(request, timeout=min(inference_timeout + 5, remaining)) as response:
                result = json.loads(response.read(16001))
            if (not isinstance(result, dict) or result.get("request_id") != request_id
                    or result.get("status") not in ("ok", "error")):
                raise ValueError("잘못된 서버 음성 응답입니다.")
            if result["status"] == "ok" and (
                    not isinstance(result.get("text"), str)
                    or result.get("classification") not in ("도움 필요", "도움 불필요", NEEDS_CONFIRMATION)):
                raise ValueError("잘못된 서버 인식 결과입니다.")
            if result["status"] == "error" or not result.get("text", "").strip():
                result["classification"] = NEEDS_CONFIRMATION
            return result
        except (URLError, OSError) as error:
            if (attempt or (isinstance(error, HTTPError) and error.code < 500)):
                raise RuntimeError("음성 서버 전송에 실패했습니다.") from error
            time.sleep(0.2)


def exchange(*, prompt, cli, model, input_device="default", output_device="",
             inference_timeout=40.0, silence_rms=0.0, server_url="", request_id=None, expires_at=None):
    stage = "preparation"
    endpoint = None
    try:
        if server_url:
            endpoint = remote_endpoint(server_url)
            request_id = request_id or str(uuid.uuid4())
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
                raise ValueError("invalid request_id")
            expires_at = time.time() + 90 if expires_at is None else expires_at
            if not math.isfinite(expires_at) or not 0 < expires_at - time.time() <= 120:
                raise ValueError("invalid voice request deadline")
        prompt = Path(prompt).expanduser().resolve()
        paths = [prompt] if server_url else [prompt, Path(cli).expanduser().resolve(), Path(model).expanduser().resolve()]
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(str(path))
        for command in ("mpg123", "arecord"):
            if shutil.which(command) is None:
                raise FileNotFoundError(command)
        if (not math.isfinite(inference_timeout) or inference_timeout <= 0
                or not math.isfinite(silence_rms) or silence_rms < 0):
            raise ValueError("timeout must be positive; silence_rms must be nonnegative")
        stage = "playback"
        report_progress(endpoint, request_id, expires_at, "playback")
        command = ["mpg123", "-q"]
        if output_device:
            command += ["-o", "alsa", "-a", output_device]
        run_command(command + [str(prompt)], 20.0)

        # Playback has exited successfully before the microphone is opened.
        with tempfile.TemporaryDirectory(prefix="fall_voice_") as directory:
            audio = Path(directory) / "speech.wav"
            stage = "recording"
            report_progress(endpoint, request_id, expires_at, "recording")
            run_command(["arecord", "-q", "-D", input_device, "-f", "S16_LE",
                         "-r", "16000", "-c", "1", "-d", "5", "-t", "wav", str(audio)], 10.0)
            audio_rms(audio)  # Reject a malformed recording before sending it.
            if server_url:
                stage = "upload"
                report_progress(endpoint, request_id, expires_at, "upload")
                return upload_audio(audio, endpoint=endpoint, request_id=request_id, expires_at=expires_at,
                                    inference_timeout=inference_timeout, silence_rms=silence_rms)
            stage = "recognition"
            return recognize_file(audio, cli=cli, model=model,
                                  inference_timeout=inference_timeout, silence_rms=silence_rms)
    except (KeyboardInterrupt, SystemExit):
        report_progress(endpoint, request_id, expires_at, "cancelled")
        raise
    except Exception as error:
        report_progress(endpoint, request_id, expires_at, "failed")
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
    parser.add_argument("--server-url", default="", help="설정하면 서버에서 인식·분류합니다.")
    parser.add_argument("--request-id")
    parser.add_argument("--expires-at", type=float)
    args = parser.parse_args()
    def terminate(signum, frame):
        # The ROS adapter cancels the entire process group. Unwind the WAV
        # temporary directory as well instead of leaving a recording behind.
        raise SystemExit(130)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        result = exchange(prompt=args.prompt, cli=args.whisper_cli, model=args.model,
                          input_device=args.input_device, output_device=args.output_device,
                          inference_timeout=args.inference_timeout, silence_rms=args.silence_rms,
                          server_url=args.server_url, request_id=args.request_id, expires_at=args.expires_at)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
