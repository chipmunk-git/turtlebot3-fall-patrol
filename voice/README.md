# 음성 입출력: Python 3.10 공용 가상환경

TTS·STT·서버는 **Python 3.10**으로 만든 **`backend/.venv` 하나**로 실행한다.
TTS 작업 파일은 `voice/tts/`, STT 작업 파일은 `voice/stt/`에 둔다.
환경 생성과 패키지 설치는 사용자가 Mac 터미널에서 직접 수행한다.

## 구성

```text
voice/
├── README.md
├── requirements.txt
├── tts/                         # 안내 MP3
└── stt/                         # 녹음·인식·분류·모의 테스트
    └── .models/                 # 로컬 Whisper 모델 캐시, Git 제외
```

TTS 파일 목록과 설정은 [tts/README.md](tts/README.md), 녹음·인식·분류 사용법은 [stt/README.md](stt/README.md)를 참고한다.
Whisper 모델 캐시는 `voice/stt/.models/`에 저장하며 Git에 포함하지 않는다.

## 최초 준비

[프로젝트 README](../README.md)의 **Mac 개인 개발 환경 (Python 3.10)** 절차를 따른다.
Python 3.10이 설치되어 있고 터미널에서 `python3.10`을 사용할 수 있다는 전제다.
준비된 Python 3.10의 기본 `venv`로 `backend/.venv`를 만든다.
이미 해당 환경이 있으면 다시 생성하지 않고 Python 버전이 `3.10.x`인지 확인한다.

프로젝트 루트에서 `backend/requirements.txt`와 `voice/requirements.txt`를 함께 설치한다.
검토한 PyTorch·NumPy·Numba·llvmlite 버전과 전체 설치 명령은 프로젝트 README를 참고한다.
`requirements.txt`는 edge-tts 7.3.1과 STT 의존성 목록을 포함한다.
설치가 실패하면 출력된 오류를 확인한다. 기존 환경을 자동으로 삭제·이동하거나 덮어쓰지 않는다.

## 같은 Python으로 실행

프로젝트 루트에서 환경 경로를 직접 지정할 수 있다.

```bash
cd ~/robotech/project_ws
backend/.venv/bin/python --version
backend/.venv/bin/python -m edge_tts --version
backend/.venv/bin/python voice/stt/record_korean.py
backend/.venv/bin/python voice/stt/record_korean.py --list-devices
```

활성화 방식으로 사용할 때:

```bash
cd ~/robotech/project_ws
source backend/.venv/bin/activate
python --version
python -m edge_tts --version
python voice/stt/record_korean.py
```

서버를 함께 실행할 때는 다른 터미널에서도 같은 환경을 활성화한다.

```bash
cd ~/robotech/project_ws
source backend/.venv/bin/activate
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

폴더를 바꿀 때마다 가상환경을 전환할 필요는 없다.
작업 파일은 `voice/tts/`, `voice/stt/`에 두고 Whisper 모델 캐시는 `voice/stt/.models/`를 사용한다.

## STT 실행 시 일어나는 일

공용 환경을 활성화한 뒤 프로젝트 루트에서 `python voice/stt/record_korean.py`를 실행한다.

1. 기본 입력 마이크의 설정을 확인하고 장치 이름을 출력한다.
2. 로컬 CPU에서 Whisper 다국어 `base` 모델을 준비한다. 최초 실행에는 모델을 다운로드하고,
   이후에는 `voice/stt/.models/`의 캐시를 재사용한다.
3. `5초 녹음 시작`이 출력되면 한국어로 말한다. 권한 요청이 나타나면 터미널 앱의 마이크 접근을 허용한다.
4. 녹음이 끝나면 Whisper로 한국어 문장을 인식하고 등록된 표현을 분류한다.
5. 인식 문장·응답 분류·음성 인식 처리 시간·녹음 소요 시간·모델 준비 시간을 출력하고 종료한다.

녹음은 메모리에서 처리하며 녹음 파일을 저장하지 않는다. 음성을 외부 인식 API로 전송하지 않는다.
이 명령은 음성 재생·로봇 제어·서버 이벤트 생성을 하지 않는다.
장치 선택과 등록 표현 분류는 [stt/README.md](stt/README.md)를 참고한다.

## 공용 환경 검증

```bash
cd ~/robotech/project_ws
backend/.venv/bin/python -m pip check
backend/.venv/bin/python -B -c 'import edge_tts, whisper, torch, numpy, sounddevice, fastapi, uvicorn, sqlalchemy, python_multipart, yaml; print("TTS/STT/서버 import OK")'
backend/.venv/bin/python -B -m unittest discover -s voice/stt -p 'test_*.py' -v
```

정상 결과는 `No broken requirements found.`, `TTS/STT/서버 import OK`, 테스트 `18개`의 `OK`다.
모의 테스트는 실제 음성 합성 서비스 연결·녹음·모델 추론을 검증하지 않는다.
STT 실동작은 녹음 프로그램으로 확인한다.
