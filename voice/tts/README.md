# TTS 안내 음성

안내 MP3는 `voice/tts/`에 둔다.

| 파일 | 용도 |
|---|---|
| `observe.mp3` | 관찰 안내 |
| `alert.mp3` | 경고 안내 |

음성 설정의 목표는 `ko-KR-SunHiNeural`, 속도 `+5%`다.
현재 포함된 MP3의 실제 생성 설정은 확인되지 않았다.

## 공용 환경

TTS 도구는 프로젝트 루트에서 `backend/.venv`로 실행한다.

```bash
cd ~/robotech/project_ws
backend/.venv/bin/python -m edge_tts --version
```

설치와 STT·서버 실행은 [음성 입출력 README](../README.md)를 참고한다.
