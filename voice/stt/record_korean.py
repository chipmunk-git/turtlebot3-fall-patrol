#!/usr/bin/env python3
"""Record five seconds from a microphone and transcribe Korean locally."""

import argparse
import sys
import time
from pathlib import Path

if __package__:
    from .response_classifier import classify_response
else:
    from response_classifier import classify_response

SAMPLE_RATE = 16000
RECORD_SECONDS = 5
MODEL_NAME = "base"
MODEL_DIR = Path(__file__).resolve().parent / ".models"


class RecognitionError(Exception):
    """A recording or transcription failure, distinct from an uncertain answer."""

    def __init__(self, stage, error):
        super().__init__(str(error))
        self.stage = stage


class KoreanRecognizer:
    """Prepare a model once, then record and classify individual responses.

    ``progress`` optionally receives human-readable progress messages. Calls to
    ``recognize`` are blocking and must be made one at a time.
    """

    def __init__(self, device=None, progress=None):
        import sounddevice as sd

        self.sd = sd
        self.device = device
        self.progress = progress
        sd.check_input_settings(
            device=device, samplerate=SAMPLE_RATE, channels=1, dtype="float32"
        )
        info = sd.query_devices(device, "input")
        self._report(f"입력 장치: {info['name']}")

        import whisper

        self._report(f"Whisper {MODEL_NAME} 준비 중 (최초 실행 시 모델 다운로드)...")
        started = time.perf_counter()
        self.model = whisper.load_model(MODEL_NAME, device="cpu", download_root=str(MODEL_DIR))
        self.model_seconds = time.perf_counter() - started

    def _report(self, message):
        if self.progress is not None:
            self.progress(message)

    def _stop_recording(self):
        try:
            self.sd.stop()
        except Exception:
            pass  # Keep the original recording error or interruption.

    def recognize(self):
        self._report(f"\n{RECORD_SECONDS}초 녹음 시작. 지금 한국어로 말씀하세요.")
        started = time.perf_counter()
        try:
            audio = self.sd.rec(
                SAMPLE_RATE * RECORD_SECONDS,
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self.device,
            )
            status = self.sd.wait(ignore_errors=False)
            if status:
                raise RuntimeError(f"녹음 중 오디오 데이터 유실: {status}")
        except KeyboardInterrupt:
            self._stop_recording()
            raise
        except Exception as error:
            self._stop_recording()
            raise RecognitionError("recording", error) from error
        record_seconds = time.perf_counter() - started

        self._report("녹음 완료. 한국어 인식 중...")
        started = time.perf_counter()
        try:
            result = self.model.transcribe(
                audio.reshape(-1), language="ko", task="transcribe", fp16=False, verbose=None
            )
            transcribe_seconds = time.perf_counter() - started
            text = result["text"].strip()
            classification = classify_response(text)
        except Exception as error:
            raise RecognitionError("recognition", error) from error

        return {
            "status": "ok",
            "text": text,
            "classification": classification,
            "record_seconds": record_seconds,
            "transcribe_seconds": transcribe_seconds,
            "model_seconds": self.model_seconds,
        }


def main():
    parser = argparse.ArgumentParser(description="마이크로 5초 녹음 후 Whisper 한국어 인식")
    parser.add_argument("--list-devices", action="store_true", help="오디오 장치 목록만 표시")
    parser.add_argument("--device", type=int, help="입력 장치 번호 (생략하면 기본 마이크)")
    args = parser.parse_args()

    try:
        if args.list_devices:
            import sounddevice as sd

            print(sd.query_devices())
            return 0

        recognizer = KoreanRecognizer(
            device=args.device, progress=lambda message: print(message, flush=True)
        )
        result = recognizer.recognize()
        print(f"\n인식 문장: {result['text'] or '(인식된 문장 없음)'}")
        print(f"응답 분류: {result['classification']}")
        print(f"처리 시간(음성 인식): {result['transcribe_seconds']:.2f}초")
        print(f"녹음 소요 시간: {result['record_seconds']:.2f}초")
        print(f"모델 준비 시간: {result['model_seconds']:.2f}초")
        return 0
    except ImportError as error:
        print(f"의존성 로딩 실패: {error}", file=sys.stderr)
        print(
            "Python 3.10 공용 환경 backend/.venv에 voice/requirements.txt를 설치해 주세요.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"실행 실패: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
