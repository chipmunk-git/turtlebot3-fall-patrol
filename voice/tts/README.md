# TTS 안내 음성

안내 MP3는 `voice/tts/`에 둔다.

| 파일 | 용도 |
|---|---|
| `observe.mp3` | 관찰 안내 |
| `alert.mp3` | 경고 안내 |

음성 설정의 목표는 `ko-KR-SunHiNeural`, 속도 `+5%`다.
현재 포함된 MP3의 실제 생성 설정은 확인되지 않았다.

## Raspberry Pi 재생 시험과 목표 경로

2026년 10월 26~27일 사용자 실기 시험에서 Raspberry Pi 4B / Ubuntu 22.04의 USB 스피커로 두 안내 MP3를 `mpg123`으로 재생하는 데 성공했다. 상세 결과는 아래 시험 기록을 참고한다.

앞으로의 Pi 목표 경로는 `~/fall_patrol/project_ws/voice/tts/`이다. **필요한 파일의 이전·배치와 새 경로에서의 재생 검증은 아직 완료되지 않았다.**
두 MP3와 `mpg123`, USB 출력 장치가 준비된 후 새 경로에서 확인할 재생 명령은 아래에 정리한다.

이미 생성된 MP3의 재생에는 Python 가상환경이나 저장소 전체가 필수인 것은 아니다.
서버·로봇 작업 경로의 구분은 [프로젝트 README](../../README.md#실행-환경과-작업-경로)를 따른다.

## Raspberry Pi USB 스피커 시험 기록 (2026-10-26~27)

### 장치 및 최초 관측값

- 시험 환경: TurtleBot3 Waffle-Pi, Raspberry Pi 4B, Ubuntu 22.04
- USB 오디오 장치: Jieli Technology `UACDemoV1.0` (`lsusb`: `4c4a:4155`)
- 관측 당시 ALSA 출력 장치: `card 1, device 0`, 카드 식별자 `UACDemoV10`
- PCM 믹서: 재생 음량 및 음소거 제어 지원
- 변경 전 최초 관측 음량: **30%** (공장 출고 기본값으로 확인한 값은 아니다.)

카드 번호 `1`은 시험 당시의 값이며 재연결·재부팅 후 달라질 수 있다.
카드 식별자도 현재 장치 목록에서 다시 확인해야 한다. 동일 모델이 여러 개라면 실제 스피커와 현재 카드 식별자의 대응도 확인한다.

### 검증 현황

아래는 사용자 시험 기록에 따른 결과이다. 명령행 재생, Python 호출, 음성 전체 연동을 구분한다.

| 검증 항목 | 결과 | 환경 및 비고 |
|---|---|---|
| `observe.mp3`, `alert.mp3` 재생 | 성공 | WSL2 Ubuntu, `mpg123` |
| Python `subprocess.run()` 재생 및 정상 종료 | 성공 | WSL2 Ubuntu, 당시 터미널에서 실행한 일회성 코드 |
| USB 스피커 인식 | 성공 | Raspberry Pi, `lsusb`, `aplay -l` |
| PCM 음량 조회 | 성공 | Raspberry Pi, `amixer -c 1 sget PCM` |
| 실제 테스트 신호 출력 | 성공 | Raspberry Pi, `speaker-test` 440Hz |
| 두 안내 MP3의 실제 재생 | **성공** | Raspberry Pi USB 스피커로 청취 확인 (2026-10-27) |
| SSH를 통한 재생 명령 실행 | 성공 | 명령은 SSH로 실행하고 소리는 Raspberry Pi의 USB 스피커에서 확인 |
| Raspberry Pi에서 Python 재생 호출 | 미시험 | 명령행 재생 성공과 별도로 검증 필요 |
| USB 스피커 분리·재연결 후 재생 | 미시험 | 장치 번호·식별자와 출력 확인 필요 |
| 60% 설정 후 재부팅 시 음량 유지 | 미확정 | 부팅 후 30%로 돌아가는 듯하다는 관찰 메모가 있으며 재시험 필요 |
| 장시간·반복 재생 안정성 | 미시험 | 반복 재생과 다른 프로세스 동시 실행 시 확인 필요 |
| Pi 마이크 녹음·Whisper 한국어 인식·전체 음성 연동 | 미시험 | USB 마이크의 ALSA 장치 인식까지만 확인 |

실제 청취 경험을 바탕으로 **60%를 운영 음량 후보로 검토 중**이며, 확정 운영값이나 재부팅 후 유지되는 값으로 간주하지 않는다.

## Raspberry Pi 확인 및 재생 명령

### 장치·음량 확인 (읽기 전용)

다음 명령은 **Raspberry Pi에서** 실행한다. 플레이어 설치 위치, 출력 모듈 목록의 `alsa`, 현재 출력 장치 번호와 식별자를 확인한다.

```bash
command -v mpg123
mpg123 --list-modules
aplay -l
aplay -L
```

USB 스피커가 현재도 `card 1`인 경우의 PCM 조회 예시다. 다른 번호라면 그 번호로 바꾼다.

```bash
amixer -c 1 sget PCM
```

### 음량 변경 (선택)

다음 명령은 **실제 음량을 변경**한다. 현재 USB 스피커가 `card 1`인지 확인하고, 현장에서 음량을 조절할 필요가 있을 때만 실행한다. 60%는 운영 후보 값이다.

```bash
amixer -c 1 sset PCM 60%
```

### 새 목표 경로에서 재생 (파일 배치 후 재검증)

다음은 `~/fall_patrol/project_ws/voice/tts/`에 두 MP3가 배치된 뒤 실행할 **미검증 목표 명령 예시**이다.
`mpg123`에 ALSA 출력과 카드 식별자를 지정한다. 현재 식별자가 `UACDemoV10`, 출력 장치가 `device 0`인지 확인하고, 다르면 실제 값으로 바꾼다.

```bash
cd ~/fall_patrol/project_ws/voice/tts
mpg123 -q -o alsa -a 'plughw:CARD=UACDemoV10,DEV=0' observe.mp3
mpg123 -q -o alsa -a 'plughw:CARD=UACDemoV10,DEV=0' alert.mp3
```

위 옵션은 해당 `mpg123` 실행에 사용할 출력 장치를 선택한다. 시스템 기본 출력 장치를 바꾸거나 `voice.dialogue`의 설정을 변경하지 않는다.
현재 `voice.dialogue`는 Linux에서 `mpg123 -q <음원 경로>`로 기본 출력 장치를 사용한다. 위 ALSA 옵션을 전달하는 기능은 없으므로, 수동 USB 재생 성공과 전체 프로그램의 USB 출력 검증을 구분한다.
프로그램에 카드 숫자 번호나 특정 환경의 오디오 경로를 고정하지 않는다.

명령 참고: [mpg123 매뉴얼](https://manpages.debian.org/bookworm/mpg123/mpg123.1.en.html), [amixer 매뉴얼](https://manpages.ubuntu.com/manpages/jammy/man1/amixer.1.html), [ALSA PCM 장치 설명](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm_plugins.html).

## 향후 확인 사항

- 재연결·재부팅 전후의 장치 번호·식별자, 음량·음소거 상태와 실제 재생을 비교한다. 음량 유지 여부를 확인하기 전에는 자동 복원이나 시스템 설정 변경을 추가하지 않는다.
- Pi에서의 Python 재생 호출과 장시간·반복 재생을 확인한다.
- `voice.dialogue`에 음원·플레이어·출력 장치를 확인하거나 선택하는 기능이 필요한지는 실제 시험 후 판단한다. 점검 중 음량을 자동 변경하지 않는다.
- USB 마이크의 실제 녹음, Whisper 한국어 인식·처리 시간·부하, 안내 재생 후 녹음·인식 전체 흐름을 별도로 검증한다. Whisper의 Pi/원격 Ubuntu PC 실행 위치는 아직 미정이다.

개인 개발 환경 준비는 [프로젝트 README](../../README.md#개인-개발-환경-python-310), 음성 프로그램 실행은 [voice/README.md](../README.md)를 참고한다.
