"""Play one prompt, then record and return a classified Korean response."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .stt.record_korean import KoreanRecognizer, RecognitionError

DEFAULT_PROMPT = Path(__file__).resolve().parent / "tts" / "observe.mp3"


def ask_once(prompt=DEFAULT_PROMPT, *, device=None, player=None, recognizer=None):
    """Return an ``ok`` response or an ``error`` with its stage and message.

    A prepared ``KoreanRecognizer`` can be reused across sequential calls. Its
    input device is fixed at preparation time, so do not also pass ``device``.
    This function waits for the player process before starting the recording.
    KeyboardInterrupt propagates to the caller; no audio files are recorded.
    """
    stage = "playback"
    try:
        prompt = Path(prompt).expanduser().resolve()
        if not prompt.is_file():
            raise FileNotFoundError(f"안내 음성 파일을 찾을 수 없습니다: {prompt}")
        player = player or ("afplay" if sys.platform == "darwin" else "mpg123")
        if player not in ("afplay", "mpg123"):
            raise ValueError("재생 프로그램은 afplay 또는 mpg123이어야 합니다.")

        stage = "preparation"
        if recognizer is None:
            recognizer = KoreanRecognizer(device=device)
        elif device is not None:
            raise ValueError("준비한 recognizer의 입력 장치는 생성할 때 지정해 주세요.")

        stage = "playback"
        command = [player, str(prompt)]
        if player == "mpg123":
            command.insert(1, "-q")
        subprocess.run(command, check=True, capture_output=True, text=True)

        stage = "recognition"
        return recognizer.recognize()
    except RecognitionError as error:
        return {"status": "error", "stage": error.stage, "message": str(error)}
    except Exception as error:
        message = str(error)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            message = error.stderr.strip() or message
        return {"status": "error", "stage": stage, "message": message}


def main():
    parser = argparse.ArgumentParser(description="안내 음성 재생 후 5초 녹음·한국어 인식")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT, help="재생할 MP3 경로")
    parser.add_argument("--device", type=int, help="입력 장치 번호 (생략하면 기본 마이크)")
    parser.add_argument(
        "--player", choices=("afplay", "mpg123"), help="기본값: Mac afplay, Linux mpg123"
    )
    args = parser.parse_args()
    try:
        result = ask_once(args.prompt, device=args.device, player=args.player)
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
