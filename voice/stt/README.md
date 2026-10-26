# Mac 마이크 → Whisper 한국어 인식

기본 입력 마이크로 5초를 녹음하고 한국어 인식 문장, 등록 표현 분류, 처리 시간을 출력한다.
ROS2·Arduino·서버 연결 없이 로컬 CPU에서 실행한다. 기본 모델은 다국어 `base`다.

## 최초 준비

TTS·STT·서버는 **Python 3.10**의 **`backend/.venv` 하나**를 공용으로 사용한다.
환경 생성·설치는 사용자가 Mac 터미널에서 수행한다.
Python 3.10 확인과 최초 환경 생성·통합 의존성 설치는 [프로젝트 README](../../README.md)의 Mac 개인 개발 환경 안내를 따른다.
음성 입출력의 공용 환경 사용법은 [../README.md](../README.md)를 참고한다.
준비된 환경은 아래 명령으로 버전과 의존성 상태를 확인한다.

```bash
cd ~/robotech/project_ws
backend/.venv/bin/python --version  # Python 3.10.x
backend/.venv/bin/python -m pip check
```

설치가 실패하면 오류를 확인하며, 남아 있는 기존 환경을 삭제하거나 덮어쓰지 않는다.

## 실행

```bash
cd ~/robotech/project_ws
backend/.venv/bin/python voice/stt/record_korean.py
```

또는 공용 환경을 활성화하고 실행한다.

```bash
cd ~/robotech/project_ws
source backend/.venv/bin/activate
python voice/stt/record_korean.py
```

먼저 기본 입력 마이크 이름을 출력하고, Whisper 다국어 `base` 모델을 준비한다.
최초 실행은 인터넷으로 모델을 받아 `voice/stt/.models/`에 저장한다.
`5초 녹음 시작`이 출력되면 한국어로 말한다. 마이크 접근 요청이 나타나면 허용한다.
접근을 거부했다면 macOS 시스템 설정 → 개인정보 보호 및 보안 → 마이크에서
실행에 사용하는 터미널 앱의 권한을 확인한다.
녹음이 끝나면 한국어 인식 문장, 응답 분류, 시간들을 출력하고 종료한다.
이 명령으로 음성이 재생되거나 로봇·서버 상태가 변경되지는 않는다.

장치 선택이 필요할 때:

```bash
backend/.venv/bin/python voice/stt/record_korean.py --list-devices
backend/.venv/bin/python voice/stt/record_korean.py --device 1
```

`1`은 예시다. 목록에서 Mac 내장 마이크의 실제 입력 장치 번호로 바꾼다.

`처리 시간(음성 인식)`은 녹음 완료 후의 Whisper 추론 시간이다.
모델 다운로드·로딩과 녹음 시간은 별도로 출력한다.
녹음은 16 kHz·모노·float32 배열로 메모리에서 처리하며 녹음 파일을 저장하지 않는다.
Whisper에 파일 경로를 전달하지 않으므로 이 프로그램의 인식 경로에는 ffmpeg가 필요하지 않다.
음성을 외부 인식 API로 전송하지 않으며 API 키가 필요하지 않다.
빈 인식 결과는 `(인식된 문장 없음)`으로 표시한다. 무음·잡음에서도 모델이 문장을 만들 수
있으므로, 이 출력만으로 실제 발화 여부나 도움 요청 여부를 판정하지 않는다.

## 등록 표현 분류

추가 모델 없이 `response_classifier.py`의 등록 표현과 전체 문장이 일치할 때만 분류한다.
띄어쓰기·탭·줄바꿈과 문장 끝의 `. ! ? 。 ！ ？ …`는 무시한다.
문장 중간의 문장부호는 유지하며, 단어 포함 검사나 부정문 의미 해석은 하지 않는다.

| 등록 표현 | 출력 분류 |
|---|---|
| 도와주세요 | 도움 필요 |
| 사람 좀 불러주세요 | 도움 필요 |
| 괜찮아요 | 도움 불필요 |
| 도움이 필요 없어요 | 도움 불필요 |

예시 출력:

```text
인식 문장: 도와 주세요!
응답 분류: 도움 필요
처리 시간(음성 인식): 2.35초
녹음 소요 시간: 5.01초
모델 준비 시간: 1.42초
```

시간은 예시다. 기존 음성 인식 처리 시간은 분류 시간을 포함하지 않는다.
`혼자 일어날 수가 없어요`, `도와달라는 뜻은 아니에요`,
`괜찮아요. 그런데 못 일어나겠어요` 등 미등록 문장은 모두 `확인 필요`다.
빈 인식 결과도 `확인 필요`로 표시하며, 이를 무응답이라고 단정하지 않는다.
녹음·인식 오류로 실패한 경우에는 응답 분류를 출력하지 않는다.
이 분류는 서버 이벤트 상태를 바꾸지 않는다.

## Python 함수로 재사용

`record_korean.py`의 기존 터미널 실행은 그대로 사용할 수 있습니다.
다른 Python 프로그램에서는 프로젝트 루트를 모듈 검색 경로에 두고 다음처럼 호출합니다.

```python
from voice.stt.record_korean import KoreanRecognizer

recognizer = KoreanRecognizer()  # 입력 장치 확인 및 모델 준비
result = recognizer.recognize()  # 5초 녹음 → 인식·분류 → dict 반환
```

준비한 객체는 여러 응답에 재사용할 수 있습니다. `device=...`로 입력 장치를 선택하며,
호출은 순서대로 실행합니다. 정상 결과에는 `status`, `text`, `classification`,
`record_seconds`, `transcribe_seconds`, `model_seconds`가 들어 있습니다.
`model_seconds`는 객체를 처음 준비할 때 걸린 시간입니다. 기본적으로 진행 메시지는
출력하지 않으며, 필요하면 생성할 때 `progress=print` 등 출력 함수를 지정합니다.

준비 중 실패는 예외로 전달됩니다. 녹음·인식 실패는 `RecognitionError`로 전달하며
`stage`는 `recording` 또는 `recognition`입니다. 녹음 중 실패나 `Ctrl+C`가 발생하면
입력을 중단합니다. 안내 MP3 재생과 실패 결과 반환까지 연결하는 방법은
[음성 입출력 README](../README.md)의 안내 재생 후 응답 인식 절차를 참고합니다.

## 마이크·모델 설치 없이 로직 테스트

```bash
cd ~/robotech/project_ws
backend/.venv/bin/python -B -m unittest discover -s voice/stt -p 'test_*.py' -v
```

테스트는 등록 표현·미등록 표현·부정문·모순 표현·정규화와 STT 결과의 분류 출력을 검사한다.
기존 녹음 설정·한국어 인식 호출·시간 출력·오류 처리도 모의 객체로 검사한다.
실제 녹음 성공 여부와 인식 정확도는 위 실행 명령으로 별도 확인한다.

공식 참고:
- https://github.com/openai/whisper
- https://github.com/openai/whisper/blob/main/whisper/audio.py
- https://python-sounddevice.readthedocs.io/en/0.5.3/installation.html
- https://python-sounddevice.readthedocs.io/en/0.5.3/api/convenience-functions.html
