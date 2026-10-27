# 음성 입출력

TTS 작업 파일은 `voice/tts/`, STT 작업 파일은 `voice/stt/`에 둔다.

개인 개발 환경의 생성·설치·활성화와 서버 실행은 [최상위 README의 개인 개발 환경](../README.md#개인-개발-환경-python-310)을 따른다.
서버·Pi 작업 경로는 [최상위 README](../README.md#실행-환경과-작업-경로), Pi MP3 재생 시험 결과와 새 목표 경로의 검증 상태는 [TTS README](tts/README.md)를 참고한다.
Pi USB 마이크는 ALSA 장치 인식까지만 확인했다. 실제 녹음·Whisper 한국어 인식·처리 시간·전체 연동은 아직 검증하지 않았으며, Whisper를 Pi와 원격 Ubuntu PC 중 어디서 실행할지는 미정이다.

## 구성

```text
voice/
├── README.md
├── requirements.txt
├── dialogue.py                  # 안내 재생 → 녹음·인식 → 결과 반환
├── test_dialogue.py             # 장비 없는 연결부 모의 테스트
├── tts/                         # 안내 MP3
└── stt/                         # 녹음·인식·분류·모의 테스트
    └── .models/                 # 로컬 Whisper 모델 캐시, Git 제외
```

TTS 파일 목록과 설정은 [tts/README.md](tts/README.md), 녹음·인식·분류 사용법은 [stt/README.md](stt/README.md)를 참고한다.
Whisper 모델 캐시는 `voice/stt/.models/`에 저장하며 Git에 포함하지 않는다.

## 실행 전제

아래 명령은 의존성이 준비된 **개인 저장소의 프로젝트 루트**에서 실행한다.
`backend/.venv/bin/python`으로 공용 환경을 직접 지정하므로 별도 활성화 명령을 반복하지 않는다.
이 명령들은 개인 개발용이며, Pi의 최종 Python 환경이나 배치 방식을 지정하지 않는다.

## STT 실행 시 일어나는 일

```bash
backend/.venv/bin/python voice/stt/record_korean.py
```

1. 기본 입력 마이크의 설정을 확인하고 장치 이름을 출력한다.
2. 로컬 CPU에서 Whisper 다국어 `base` 모델을 준비한다. 최초 실행에는 모델을 다운로드하고,
   이후에는 `voice/stt/.models/`의 캐시를 재사용한다.
3. `5초 녹음 시작`이 출력되면 한국어로 말한다. 권한 요청이 나타나면 터미널 앱의 마이크 접근을 허용한다.
4. 녹음이 끝나면 Whisper로 한국어 문장을 인식하고 등록된 표현을 분류한다.
5. 인식 문장·응답 분류·음성 인식 처리 시간·녹음 소요 시간·모델 준비 시간을 출력하고 종료한다.

녹음은 메모리에서 처리하며 녹음 파일을 저장하지 않는다. 음성을 외부 인식 API로 전송하지 않는다.
이 명령은 음성 재생·로봇 제어·서버 이벤트 생성을 하지 않는다.
장치 선택과 등록 표현 분류는 [stt/README.md](stt/README.md)를 참고한다.

## 안내 재생 후 응답 인식

현재 `voice.dialogue`는 **실행한 컴퓨터에서 재생·녹음·Whisper 인식을 모두 수행**한다. 원격 PC로 음성을 보내 추론하는 기능은 구현되어 있지 않다.
이 명령은 `voice/`의 관련 소스·안내 파일과 Python 의존성이 준비된 환경에서 사용하는 개발용 실행 방법이며, Pi의 최종 배치·실행 위치를 확정하는 안내는 아니다.

프로젝트 루트에서 다음 명령을 실행하면 Whisper 모델을 준비하고, 기존 `observe.mp3`를
재생한 뒤 5초 녹음·한국어 인식·등록 표현 분류를 수행한다. 실제 음성이 재생되고
마이크 입력을 사용하므로 직접 확인할 수 있을 때 실행한다.

```bash
backend/.venv/bin/python -m voice.dialogue
```

Mac에서는 기본 제공 `afplay`, Linux에서는 `mpg123`을 기본 재생 프로그램으로 사용한다.
Linux에서 `mpg123`이 없다면 사용자가 별도로 준비해야 한다. 자동 설치나 음량·ALSA 설정
변경은 하지 않는다. 실제 Pi의 출력 장치와 음량은 현장에서 별도로 확인해야 한다.
기본 안내 파일은 소스 위치를 기준으로 찾는다. 사용자 지정 `--prompt`의 상대 경로는
명령을 실행한 디렉터리를 기준으로 해석한다.

기존 STT의 장치 목록 명령으로 입력 장치 번호를 확인하고, 필요할 때만 지정한다.

```bash
backend/.venv/bin/python voice/stt/record_korean.py --list-devices
backend/.venv/bin/python -m voice.dialogue --device 1
```

`1`은 예시다. `--prompt voice/tts/alert.mp3`로 다른 안내 파일을 지정할 수 있으며,
`--player afplay` 또는 `--player mpg123`으로 재생 프로그램을 선택할 수 있다.
재생 프로세스의 정상 종료를 기다린 후 녹음을 시작한다. 실제 장치의 잔여 출력 지연은
하드웨어 시험으로 확인해야 한다.

성공하면 다음 형태의 JSON을 출력하고 종료 코드 `0`으로 끝난다. 시간은 예시다.

```json
{
  "status": "ok",
  "text": "도와주세요.",
  "classification": "도움 필요",
  "record_seconds": 5.01,
  "transcribe_seconds": 2.35,
  "model_seconds": 1.42
}
```

처리에 실패하면 `status: "error"`, `stage`, `message`를 반환하고 종료 코드 `1`로 끝난다.
실패 결과에는 응답 분류를 넣지 않는다. 단계는 다음과 같다.

| `stage` | 의미 |
|---|---|
| `preparation` | 입력 장치 설정 확인 또는 모델 준비 실패 |
| `playback` | 안내 파일·재생 프로그램 확인 또는 재생 실패 |
| `recording` | 녹음 실패 또는 입력 데이터 유실 |
| `recognition` | 음성 인식·결과 처리 실패 |

재생이 실패하면 녹음을 시작하지 않는다. 미등록 문장이나 빈 인식 결과는
`status: "ok"`, `classification: "확인 필요"`로 반환한다. 빈 결과만으로 실제 무응답을
판정하지 않는다. `Ctrl+C`는 종료 코드 `130`으로 끝나며, 녹음 중이면 입력을 중단한다.
녹음 파일을 저장하거나 ROS2·서버 상태를 변경하지 않는다. 자동 재시도·시간 제한은
이 연결부에 포함하지 않는다.

## 다른 Python 프로그램에서 호출

프로젝트 루트가 Python 모듈 검색 경로에 있는 환경에서 호출한다.

```python
from voice.dialogue import ask_once

result = ask_once()  # 모델 준비 → 안내 재생 → 녹음·인식
if result["status"] == "ok":
    print(result["text"], result["classification"])
else:
    print(result["stage"], result["message"])
```

반복 호출할 때는 모델을 한 번 준비해 재사용할 수 있다.

```python
from voice.dialogue import ask_once
from voice.stt.record_korean import KoreanRecognizer

recognizer = KoreanRecognizer()  # 입력 장치 확인 및 모델 준비
first = ask_once(recognizer=recognizer)
second = ask_once(recognizer=recognizer)
```

입력 장치는 `KoreanRecognizer(device=...)`를 생성할 때 지정하며, 준비한 객체와
`ask_once(device=...)`를 함께 넘기지 않는다. 직접 생성한 객체의 준비 실패는 호출자가
예외로 처리해야 한다. `recognizer.recognize()`는 안내 재생 없이 녹음·인식 결과를 반환하며,
실패하면 실패 단계가 담긴 `RecognitionError`를 발생시킨다. 호출은 순서대로 실행한다.
`ask_once()`의 `KeyboardInterrupt`는 호출한 프로그램으로 전달된다.
음성 결과를 로봇 행동이나 서버 이벤트에 어떻게 반영할지는 호출 프로그램에서 결정한다.

## 연결부 모의 테스트

```bash
backend/.venv/bin/python -B -m unittest voice.test_dialogue -v
```

재생·마이크·Whisper를 모의 객체로 대체한다. 모델 준비와 재생 완료 후 녹음이 시작되는지,
모델 재사용, 결과 반환, 준비·재생·녹음·인식 실패 및 중단 처리를 검사한다.
실제 음성 재생·녹음·모델 다운로드나 추론은 수행하지 않는다.

정상 결과는 `Ran 23 tests`와 `OK`이다.

## STT 모의 테스트

```bash
backend/.venv/bin/python -B -m unittest discover -s voice/stt -p 'test_*.py' -v
```

정상 결과는 `Ran 18 tests`와 `OK`이다.
모의 테스트는 실제 음성 합성 서비스 연결·녹음·모델 추론을 검증하지 않는다.
STT 실동작은 녹음 프로그램으로 확인한다.
