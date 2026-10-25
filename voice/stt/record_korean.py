#!/usr/bin/env python3
"""Record five seconds from a microphone and transcribe Korean locally."""

import argparse
import sys
import time
from pathlib import Path

from response_classifier import classify_response

SAMPLE_RATE = 16000
RECORD_SECONDS = 5
MODEL_NAME = "base"
MODEL_DIR = Path(__file__).resolve().parent / ".models"


def main():
    parser = argparse.ArgumentParser(description="마이크로 5초 녹음 후 Whisper 한국어 인식")
    parser.add_argument("--list-devices", action="store_true", help="오디오 장치 목록만 표시")
    parser.add_argument("--device", type=int, help="입력 장치 번호 (생략하면 기본 마이크)")
    args = parser.parse_args()
    sd = None

    try:
        import sounddevice as sd

        if args.list_devices:
            print(sd.query_devices())
            return 0

        sd.check_input_settings(
            device=args.device, samplerate=SAMPLE_RATE, channels=1, dtype="float32"
        )
        info = sd.query_devices(args.device, "input")
        print(f"입력 장치: {info['name']}", flush=True)

        import whisper

        print(f"Whisper {MODEL_NAME} 준비 중 (최초 실행 시 모델 다운로드)...", flush=True)
        started = time.perf_counter()
        model = whisper.load_model(MODEL_NAME, device="cpu", download_root=str(MODEL_DIR))
        model_seconds = time.perf_counter() - started

        print(f"\n{RECORD_SECONDS}초 녹음 시작. 지금 한국어로 말씀하세요.", flush=True)
        started = time.perf_counter()
        audio = sd.rec(
            SAMPLE_RATE * RECORD_SECONDS,
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=args.device,
        )
        status = sd.wait(ignore_errors=False)
        if status:
            raise RuntimeError(f"녹음 중 오디오 데이터 유실: {status}")
        record_seconds = time.perf_counter() - started

        print("녹음 완료. 한국어 인식 중...", flush=True)
        started = time.perf_counter()
        result = model.transcribe(
            audio.reshape(-1), language="ko", task="transcribe", fp16=False, verbose=None
        )
        transcribe_seconds = time.perf_counter() - started
        text = result["text"].strip()

        print(f"\n인식 문장: {text or '(인식된 문장 없음)'}")
        print(f"응답 분류: {classify_response(text)}")
        print(f"처리 시간(음성 인식): {transcribe_seconds:.2f}초")
        print(f"녹음 소요 시간: {record_seconds:.2f}초")
        print(f"모델 준비 시간: {model_seconds:.2f}초")
        return 0
    except ImportError as error:
        print(f"의존성 로딩 실패: {error}", file=sys.stderr)
        print(
            "Python 3.10 공용 환경 backend/.venv에 voice/requirements.txt를 설치해 주세요.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        if sd is not None:
            sd.stop()
        print("\n중단했습니다.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"실행 실패: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
