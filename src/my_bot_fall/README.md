# 순찰·자세 판정과 Raspberry Pi 음성 연결

`lying_detector`가 자세 결과를 `/pose_state`에 발행하고, `patrol`이 순찰·현위치 정지 관찰·경고·웹 확인과 시작 명령 후 재개를 관리합니다. 의심 대상에게 추가 접근하지 않습니다. 자세 모델은 `lying_detector`에서 한 개만 실행합니다.

사건 사진에는 관찰 대상 한 명의 박스와 확인된 관절을 표시합니다. 원본 카메라 영상은 바꾸지 않으며, 판정 프레임과 사진을 연결하지 못하거나 그리기에 실패하면 원본 사진을 사용합니다. 자세 노드는 최근 프레임 위주로 수신하고 처리 시간 통계를 출력합니다. 실제 사진 정합성과 처리 속도는 장비에서 확인해야 합니다.

`patrol`이 `/status`와 `/events`를 직접 갱신합니다. 같은 서버에 `status_bridge`나 `fake_robot`을 동시에 실행하지 않습니다. `/cmd_vel`을 발행하는 기존 `navigation`, 다른 주행 프로그램, teleop, Nav2 controller도 동시에 실행하지 않습니다.

순찰 지점·지속된 정지와 해소·선별한 ROS2 오류는 별도 로그 전송 스레드가 `POST /logs`로 웹에 전달합니다. 서버 장애 중 최대 200건을 보관하고, 종료 시 전송 스레드는 최대 1초만 기다립니다. 반복·짧은 정지의 요약과 각 프로그램의 표시 범위는 [웹 로그 안내](../../scripts/README.md#웹에서-보는-로그)를 참고합니다.

## 연결 구조

```text
Ubuntu 서버 PC                              Pi
lying_detector → /pose_state → patrol
                                │ ── voice_request ──→ voice
                                │                       observe.mp3 재생 완료
                                │                         → 5초 녹음
FastAPI /voice/transcribe ←──── WAV·관찰 요청 ID ─────────┘
  whisper.cpp base-q8_0 → 등록 표현 분류
                    └────────── 인식·분류 JSON ────────→ voice
patrol ←────────────────────── voice_result ─────────────┘
  └─ 기존 사건 메모 PATCH → FastAPI / 대시보드
```

주행·자세 판정 노드는 기존 원격 Ubuntu PC 실행 방식을 사용할 수 있습니다. Pi는 기존 로봇 bringup·센서 송신과 음성 입출력을 맡습니다. 음성 연결 때문에 주행·추론 전체를 Pi로 이전할 필요는 없습니다.

관찰별 식별자로 음성을 한 번 실행합니다. 요청·결과가 유실돼 재전송하더라도 완료 결과를 다시 전달합니다. Pi 기록은 기본 `~/.turtlewatch/voice_requests.json`에 보관하며, 중간에 재시작한 교환은 오류로 반환하고 자동 재생하지 않습니다. **Pi 음성 노드는 한 개만 실행합니다.**

음성 결과는 기존 사건의 **메모에만** 기록합니다. 도움 요청·거절·확인 필요·실패 모두 영상 판정이나 웹의 상태 처리를 자동으로 뒤집지 않습니다. 음성 때문에 새 사건을 만들거나 자동으로 순찰을 재개하지 않습니다.

기존 사건 생성 시점인 `alert`는 유지합니다. 음성 결과가 먼저 오면 같은 관찰에 보관한 뒤 생성된 `event_id`에 연결합니다. 사건 생성 전에 정상 자세로 순찰에 복귀하거나 웹 확인으로 재개 대기에 들어가면 미완료 음성을 취소합니다. 이전 관찰의 결과는 새 사건에 반영하지 않습니다.

## 실행 전 조건

- ROS2 Humble과 기존 주행·자세 추론 의존성, bringup·카메라·지도·AMCL 환경을 준비합니다. 환경을 자동 설치하거나 변경하지 않습니다.
- Pi와 ROS2 실행 PC는 같은 ROS2 도메인과 통신 가능한 네트워크를 사용하고 시계를 동기화합니다. 음성 요청은 만료 시각을 검사합니다.
- Pi에는 `mpg123`, `arecord`가 필요합니다. 원격 인식 모드에서는 Pi의 Whisper 실행 파일·모델을 사용하지 않습니다. 기존 파일을 삭제하거나 이동할 필요는 없습니다.
- 서버 PC에는 서버에서 실행 가능한 `whisper-cli`와 기존 `ggml-base-q8_0.bin` 모델을 준비합니다. Pi용 실행 파일을 그대로 복사해 서버에서 실행할 수 있다고 가정하지 않습니다. 가능하면 Pi 시험에 사용한 `whisper.cpp` 버전과 맞춥니다.
- `arecord -l`, `aplay -l`로 실제 ALSA 장치를 확인합니다. 다른 컴퓨터의 카드 번호를 그대로 사용하지 않습니다.

서버·Pi 경로는 [최상위 README](../../README.md#실행-환경과-작업-경로)를 따릅니다. **Pi 목표 경로에 아래 파일을 배치하고 실행하는 검증은 아직 완료하지 않았습니다.** Pi에는 `my_bot_fall` 패키지, `voice/stt/whisper_session.py`, `voice/stt/response_classifier.py`, `voice/tts/observe.mp3`·`alert.mp3`를 배치합니다. 서버에도 `backend/`와 같은 프로젝트 루트 아래의 `voice/stt/whisper_session.py`·`response_classifier.py`가 필요합니다. 이 두 파일의 공통 인식 함수를 재사용하며, 서버에 마이크·스피커 프로그램이나 Python Whisper를 추가 설치할 필요는 없습니다. 저장소 전체 배포가 필수인 것은 아닙니다.

## Ubuntu PC: 빌드·서버·순찰

필요한 소스가 작업공간에 배치된 경우 빌드합니다. `--base-paths src`로 `parts/`의 같은 이름 패키지가 함께 탐색되지 않도록 합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select my_bot_fall
source install/setup.bash
```

실제 로봇의 전체 실행 순서와 확인 결과는 [인수인계 문서](../../docs/HANDOVER.md)에 정리되어 있습니다.

### 지도 제공·위치 추정 (별도 터미널)

Pi의 기존 bringup·카메라를 실행한 뒤 Ubuntu PC의 별도 터미널에서 실행합니다. `nav2_bringup`과 그 의존 패키지인 지도 서버·AMCL·lifecycle manager, `rviz2`가 준비되어 있어야 합니다. 같은 ROS2 환경에서 지도 서버·AMCL 또는 SLAM이 이미 실행 중이면 중복 실행하지 않습니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 launch my_bot_fall tb3_localization_real.launch.py \
  map:="$PWD/src/maps/new_map.yaml" \
  use_sim_time:=false
```

`map`은 필수 인자이며 지도 YAML의 절대 경로를 지정합니다. 다른 작업 경로에서는 실제 YAML 위치를 사용합니다. `src/maps/new_map.yaml`은 같은 폴더의 `new_map.pgm`을 참조하므로 두 파일을 함께 둡니다. 지도는 패키지 설치 대상이 아니며, 패키지만 따로 배포할 때도 실행 PC에 두 파일을 함께 복사하고 그 경로를 지정합니다.

이 launch는 지도 서버·AMCL과 RViz를 실행합니다. 기존 실기용 launch와 같은 `nav2_bringup/params/nav2_params.yaml`을 사용하며, 기본 `use_sim_time`은 `false`입니다. 기존 `my_bot` 패키지의 위치 추정 launch 대신 사용하고, Gazebo나 Nav2 주행 controller는 실행하지 않습니다. RViz의 **2D Pose Estimate**로 실제 위치·방향을 맞추고 `/map`·`/amcl_pose` 수신을 확인한 뒤 아래 자세·순찰 노드를 실행합니다. 이번 경로에서의 실제 로봇 위치 추정·주행은 별도 실기 검증이 필요합니다.

### FastAPI 서버

FastAPI가 실행 중이지 않을 때 서버 터미널에서 실행합니다. 아래 Whisper 경로는 준비된 파일의 실제 경로로 바꿉니다. 기존 모델이 있으면 중복 복사하거나 덮어쓰지 않고 확인합니다. 실행 파일·모델·가상환경의 설치와 배치는 사용자가 수행합니다.

```bash
cd ~/robotech/project_ws/backend
export FALL_WHISPER_CLI="$HOME/whisper.cpp/build/bin/whisper-cli"
export FALL_WHISPER_MODEL="$HOME/whisper.cpp/models/ggml-base-q8_0.bin"
test -x "$FALL_WHISPER_CLI" && test -f "$FALL_WHISPER_MODEL" && \
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

`POST /voice/transcribe`는 16 kHz·모노·16비트 PCM, 5초 이내 WAV를 받아 인식 문장·분류·추론 시간을 반환합니다. 사건 색상·로봇 상태·관리자 메모는 바꾸지 않습니다. 인식은 최대 40초와 요청 만료 시각 중 짧은 제한을 적용하고 한 번에 한 건만 실행합니다. 같은 요청 ID·녹음·만료 시각의 재전송에는 DB에 저장한 결과를 재사용합니다. 동시 인식 제한은 프로세스 단위이므로 **서버는 한 프로세스(`--workers 1`)로 실행합니다.**

서버 시작 시 기존 사건 테이블을 유지하고 음성 전용 `voice_records` 테이블만 추가합니다. 기존 DB를 삭제하거나 `seed`를 실행하지 않습니다. 순찰 노드는 기존 사건 생성 요청에 관찰의 `voice_request_id`를 함께 보내며, 먼저 도착한 음성 기록도 같은 사건에 연결합니다. 기존 사건 메모 갱신 경로는 유지합니다.

### 웹에서 음성 응답 확인

사건 상세의 **음성 응답** 영역은 관리자 메모와 별개입니다. 요청 대기 → 안내 재생 중 → 녹음 중 → 전송 중 → 인식 중 → 완료·실패·취소를 표시하고, 서버 인식이 끝나면 문장·분류를 약 1초 주기로 갱신합니다. 사건 메모에 결과가 반영될 때까지 기다리지 않습니다. 사진으로 먼저 빨강·노랑 사건을 만들더라도 음성은 별도로 처리하며, 음성 때문에 사건 색상을 바꾸지 않습니다. 실제 발화 여부가 확인된 문장이라는 뜻은 아닙니다.

인식 문장·분류와 처리 실패는 웹 활동 로그에도 저장합니다. 음성 결과 응답을 보낸 뒤 별도 DB 세션에서 기록하며, 로그 저장 실패는 서버 터미널에 경고로 표시합니다.

서버가 유효한 WAV를 받으면 프로젝트의 `voice/recordings/`에 임의 파일명으로 보관합니다. 이 폴더는 Git에서 제외하며 TTS 안내 MP3와 구분합니다. 사건 상세의 재생 컨트롤로 원음을 들을 수 있고 자동 재생은 하지 않습니다. 화면 갱신이 재생 중인 음성을 다시 시작하지 않으며, 다른 사건으로 이동하거나 상세 창을 닫으면 재생을 멈춥니다. 인식 실패 시에도 이미 받은 유효한 녹음은 재생할 수 있습니다.

- `POST /voice/{request_id}/progress`: Pi의 재생·녹음·전송·실패·취소 상태 전달
- `GET /voice/events/{event_id}`: 사건에 연결된 진행 상태·문장·분류·재생 주소 조회
- `GET /voice/{request_id}/audio`: 사건에 연결된 원음 재생 (구간 요청 지원)

녹음 폴더 전체는 정적 공개하지 않으며 사건에 연결되지 않은 원음은 재생 API로 제공하지 않습니다. 재생 API는 기존 대시보드와 같은 서버 접근 환경을 사용하며 별도 관리자 로그인 기능은 추가하지 않았습니다. 서버 재시작 뒤에도 음성 기록·원음은 남습니다. 자동 삭제는 하지 않으므로 팀의 보관 기간·정리 기준을 정해 운영해야 합니다. 아직 진행 정보가 도착하지 않은 기록도 일정 시간 뒤 실패로 표시하며, 처리가 끝난 기록을 늦은 상태 통지로 되돌리지 않습니다.

위의 지도 제공·위치 추정과 서버를 준비하고 RViz에서 실제 초기 위치를 맞춘 뒤, 자세 노드 터미널에서 실행합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run my_bot_fall lying_detector --ros-args --params-file "$(ros2 pkg prefix my_bot_fall)/share/my_bot_fall/config/params_robot.yaml"
```

`weights`가 비어 있으면 설치된 패키지의 `models/fall_pose_n.pt`를 사용합니다. 다른 모델은 `-p weights:=/absolute/path/to/model.pt`로 지정합니다. 모델이 없으면 실패하며 자동 다운로드하지 않습니다. 자세 노드가 정상인지 확인합니다.

```bash
ros2 topic echo /pose_state --once
```

순찰 터미널에서 실행합니다. 아래 서버 주소는 같은 PC에서 FastAPI가 실행될 때 유효합니다. 다른 PC라면 실제 서버 주소를 지정합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run my_bot_fall patrol --ros-args -p server_url:=http://127.0.0.1:8000 -p voice_enabled:=true
```

RViz에서 A/B/C 지점을 지정한 뒤 대시보드의 **주행 시작**을 누르면 기존 안전 조건이 충족될 때 반복 순찰합니다. **정지**는 현재 목표·경로를 취소하되 순찰 지점은 유지합니다. 다시 시작하면 현재 위치에서 같은 목적지로 경로를 재계산합니다. 지점 도착 후 대기 중에 정지했다면 다음 목적지로 재개합니다.

노드 시작·통신 복구·경고 해제 후에는 서버의 이전 `run`을 `stop`으로 초기화한 뒤 **주행 시작을 다시 눌러야 합니다.** 준비 전이나 경고 처리 중에 누른 시작 명령은 예약되지 않습니다. 경고는 사람이 웹에서 조치 완료·오탐 처리하고 기존 위치·센서 안전 조건이 충족되어야 해제됩니다.

`/control` 조회·초기화 실패나 잘못된 응답은 주행을 막지만, 기존 상태 보고·사건 생성·음성 메모 전송은 계속 시도합니다. 정지·통신 오류가 진행 중인 관찰과 음성 교환을 취소하지는 않습니다. 실제 버튼→ROS2 주행 및 통신 복구 동작은 장비에서 별도 확인이 필요합니다.

관찰 제한은 기본 15초이며 자세 판정·영상 중단 시 경고 조건은 기존 구현을 사용합니다. 음성만 제외하려면 `-p voice_enabled:=false`로 실행합니다. 음성 결과 대기 기본값은 90초이며 관찰 제한과 별개입니다.

## Raspberry Pi: 음성 실행

다음은 목표 작업공간에 필요한 파일을 배치하고 ALSA 장치와 서버의 인식 준비를 마친 뒤 확인할 명령입니다. `SERVER_IP`는 Ubuntu 서버 PC의 실제 주소로 바꿉니다.

```bash
cd ~/fall_patrol/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select my_bot_fall
source install/setup.bash
ros2 run my_bot_fall voice --ros-args \
  -p script_path:="$(pwd)/voice/stt/whisper_session.py" \
  -p server_url:=http://SERVER_IP:8000
```

기본 녹음·재생 장치는 ALSA `default`입니다. 카드 번호는 바뀔 수 있으므로 `arecord -l`, `aplay -l`의 이름으로 `-p input_device:=plughw:CARD=microphone,DEV=0`, `-p output_device:=plughw:CARD=UACDemoV10,DEV=0`처럼 지정하는 것을 권장합니다. 필요하면 실제 장치를 `-p input_device:=...`, `-p output_device:=...`로 지정하며 Python은 `-p python_executable:=...`로 지정합니다. `server_url`을 생략하면 기존 Pi 로컬 인식 시험 모드이며 이때만 `model_path`·`whisper_cli`를 사용합니다. 원격 인식 중 서버가 실패해도 Pi 로컬 추론으로 자동 전환하지 않습니다.

### 사건별 경고 안내

`observe.mp3` 재생 → 5초 녹음 → 인식·분류가 종료되고 결과가 순찰 노드에 전달된 뒤, 같은 관찰의 서버 사건이 미처리 빨강(`confirmed`)·노랑(`uncertain`)이면 `alert.mp3`를 한 번 재생합니다. 사건 생성과 음성 완료 중 늦게 끝난 쪽을 기다리며, 경고 재생 때문에 인식 결과 전달을 늦추거나 다시 녹음하지 않습니다. 음성 처리 오류 결과도 같은 조건으로 경고를 요청할 수 있지만, 순찰 노드의 대기 시간 초과만으로 Pi의 녹음 종료를 가정하지는 않습니다.

Pi는 기존 안내·녹음·인식과 경고 재생에 하나의 음성 작업 슬롯을 사용합니다. 경고는 `mpg123`으로만 재생하며 같은 `output_device`를 사용합니다. 기본 파일은 `voice/tts/alert.mp3`이고 `-p alert_prompt_path:=...`로 변경할 수 있습니다. 경고 요청·결과는 기존 음성 토픽에서 `action: alert`와 사건 ID로 구분합니다. 원래 관찰 ID·인식 문장·분류·사건 메모와 색상·주행 규칙은 유지합니다.

서버에서 해당 사건의 미처리를 확인한 뒤 요청하며, 확인이 2초 넘게 갱신되지 않으면 재전송을 멈춥니다. 경고 요청은 처음 발급한 시각부터 20초 동안만 유효하며 기한을 연장하지 않습니다. 조치 완료·오탐·관찰 종료가 전달되면 대기·재생 중 경고도 취소합니다. 통신 지연 중에는 즉시 취소를 보장하지 않습니다. Pi는 재생 시도를 저널에 먼저 남겨 재전송·재시작 때 자동 재생하지 않으며, 파일 누락·재생 실패도 같은 사건에서 자동 반복하지 않습니다. 실패는 순찰 노드 로그로 확인하고 기존 음성 결과를 덮어쓰지 않습니다.

서버 추론 시간만큼 경고가 늦어질 수 있습니다. 팝업 기반 STM32 부저 기능은 별도이며, 함께 사용할 때 녹음에 부저 소리가 섞이는지는 실기에서 확인해야 합니다. ROS2 없이 아래 단독 음성 교환 명령을 실행하면 사건별 경고는 재생하지 않습니다.

ROS2 없이 Pi 음성 교환만 수동 확인할 때는 다음 명령을 사용합니다. **실제로 음성이 재생되고 마이크로 녹음합니다.** 자동 음성 노드와 동시에 실행하지 않습니다.

```bash
cd ~/fall_patrol/project_ws
python3 voice/stt/whisper_session.py --server-url http://SERVER_IP:8000
```

성공하면 `status`, `text`, `classification`, `transcribe_seconds` 등을 JSON으로 출력하고, 실패하면 단계와 오류·`확인 필요`를 반환합니다. `transcribe_seconds`는 서버 추론 시간이며 전송 시간은 포함하지 않습니다. Pi의 WAV와 서버의 검사용 임시 WAV는 처리 후 제거하고, 유효한 서버 보관본은 `voice/recordings/`에 남깁니다. 연결 오류·서버 일시 오류에서는 같은 WAV·요청 ID로 한 번만 재전송하며 안내·녹음은 반복하지 않습니다. 진행 상태 통지가 실패해도 재생·녹음·정리를 막지 않습니다. 서버는 기존 디지털 무음 검사·등록 표현 분류를 사용하며, 일반적인 무음·작은 목소리·Whisper의 잘못된 문장 생성 문제를 해결한 것은 아닙니다. `speech_validated: false`이며 음성만으로 도움 거절을 확정하지 않습니다.

Pi에서 취소하면 전송·음성 자식 프로세스와 로컬 임시 WAV를 정리합니다. 서버 추론은 즉시 중단된다고 보장하지 않으며 제한 시간 내 종료합니다. 서버에 이미 보관한 녹음은 남기되, 취소된 기록을 늦은 추론 결과로 완료 상태로 바꾸지 않습니다. 기존 요청 ID·만료·취소 검사와 90초 음성 교환 제한은 유지합니다.

## 출력 장치 연결

브리지는 **USB 장치가 연결된 컴퓨터**에서 실행합니다. 실제 로봇은 NUCLEO가 Pi에 연결되므로 Pi에서 실행합니다. 번호는 연결 순서에 따라 바뀌므로 `ls -l /dev/serial/by-id/`로 확인한 고유 경로를 사용합니다.

```bash
cd ~/fall_patrol/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select tb3_arduino_io
source install/setup.bash
ros2 run tb3_arduino_io bridge --ros-args -p port:=$(ls /dev/serial/by-id/usb-STMicroelectronics_STM32_STLink*)
```

상태 조회 프로그램은 서버 PC에서 실행합니다. 브리지와 ROS2 토픽으로 통신합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
python3 src/dev_scripts/status_to_arduino.py --server-url http://127.0.0.1:8000
```

브리지의 `port` 기본값은 `/dev/ttyACM0`이지만, 같은 번호에 다른 장치가 잡힐 수 있으므로 지정해서 실행합니다. 브리지는 115200 baud, 줄바꿈으로 끝나는 `R,r,g,b`, `L,첫째줄|둘째줄`, `P,0/1`, `B,0/1`, `H`를 보냅니다. LCD는 표시 가능한 ASCII만 사용합니다. Arduino Uno와 STM32 NUCLEO-F103RB의 펌웨어·핀·명령 규약은 [MCU 안내](../../mcu/README.md)를 참고합니다. 두 소스에서 같은 명령 처리를 확인했으며, 실제 STM32 보드와 브리지의 전체 연동은 별도 실기 확인이 필요합니다.

상태 조회 프로그램은 RGB·LCD 문구·LCD 전원을 자동 갱신합니다. 로봇 표시는 대기(`idle`, 파랑)·순찰(`patrol`, 초록)·관찰(`observe`, 노랑)·알림(`alert`, 빨강) 네 가지입니다. 이전 `suspect`·`approach`는 표시에서만 관찰로 묶으며 서버 값·기록·주행 동작은 바꾸지 않습니다. 사건 처리 상태는 별도로 유지합니다. 부저는 로봇 상태와 별개로, 웹에 팝업이 표시된 미처리 빨강·노랑 사건을 기준으로 울립니다. 조치 완료·오탐·해당 사건의 경보음 끄기로 경보 대상이 없어지면 끕니다. 상세 창 버튼은 사건 상태·주행·음성을 바꾸지 않습니다. 기존 LCD 문구와 부저 반복음 패턴은 유지하며, 서버 조회 실패·정상 종료(Ctrl+C) 시에는 부저를 끕니다. 사건 경보 조회만 실패하면 RGB·LCD는 계속 갱신합니다. 표시 조건·API·버튼 동작은 [출력 장치 안내](../../README.md#arduino-출력-장치)를 참고하며, 실제 보드·브라우저 통합은 별도 실기 확인이 필요합니다.

## 남은 실기 확인

- ROS2 원본 `src/maps/new_map.yaml`은 해상도 0.05m·원점 `[-3.94, -1.06, 0]`입니다. 웹 지도는 같은 원본에서 파생되며 잘라내기·회전에 맞춰 원점을 보정하지만, 정리·직선화로 장애물 형상도 바뀌었습니다. 현재 웹 지도는 해상도 0.00625m·원점 `[-3.2414, -0.7697, 0.03048]`이며 ROS2 주행용으로 대체하지 않습니다. 실제 로봇·RViz·웹 위치의 정합성은 장비에서 확인해야 합니다.
- 원본의 기존 `free_thresh: 0.25`는 유지합니다. Nav2 Humble 지도 로더에서는 회색 205가 자유 공간으로 해석되므로, 실제 `/map`의 회색 영역 처리와 의도한 주행 범위를 확인해야 합니다.
- 기존 `location_ok()`는 AMCL 위치 보유 여부만 검사합니다. 오래된 위치·TF 처리와 실제 센서 중단 시 정지는 실기 확인이 필요합니다. 음성 연결을 이유로 위치 판정·주행 규칙을 교체하지 않았습니다.
- ROS2 요청 수신, 실제 스피커→마이크 순서, 작은 목소리·무음, 추론 시간, 같은 사건 메모·사진 및 사람의 확인 후 재개를 장비에서 확인해야 합니다.
- 메모는 GET 후 기존 내용에 덧붙여 PATCH하고 식별자로 재전송 중복을 막습니다. 서버에 조건부 갱신 기능이 없어 동시 웹 메모 편집과의 경쟁은 남습니다.
- 음성에 따른 사건 상태 자동 변경은 추가하지 않았습니다. 사건별 `alert.mp3` 재생은 위 조건으로 연결했으며 실제 Pi·서버 통합 재생은 별도 검증이 필요합니다.

## 장비 없는 모의 테스트

개인 개발 환경의 프로젝트 루트에서 실행합니다.

```bash
backend/.venv/bin/python -B -m unittest discover -s src/my_bot_fall/tests -p 'test_*.py' -v
backend/.venv/bin/python -B -m unittest discover -s voice -p 'test_*.py' -v
backend/.venv/bin/python -B -m unittest discover -s voice/stt -p 'test_*.py' -v
backend/.venv/bin/python -B -m unittest discover -s backend/tests -p 'test_*.py' -v
```

기존 순찰·안전 정지·오탐 처리와 음성 중복·취소·재시작·제한 시간, 같은 이벤트 메모 반영을 검사합니다. 원격 인식은 서버의 실제 ASGI 업로드와 임시 DB·녹음 폴더를 사용하고 외부 Whisper 실행만 모의합니다. 재생·녹음 후 전송 순서, 같은 녹음의 재전송, 서버 장애·잘못된 WAV·동시 추론 제한·취소 후 임시 파일 삭제, 사건별 진행·결과·원음 연결과 재시작 후 보존도 검사합니다. ROS2 통신·실제 모델 추론·하드웨어 통합 성공을 의미하지 않습니다.

2026-11-01 사용자 터미널에서 서버 관련 44개·음성 41개·순찰 패키지 57개, 총 142개 중 141개가 통과했습니다. Pillow가 설치되지 않아 기존 지도 `--pretty` 검사 한 개는 건너뛰었으며 음성 관련 실패는 없었습니다. 별도의 대시보드 모의 검사 23개도 통과했습니다.

사건별 경고 안내 연결 후 같은 날 순찰 패키지 검사 69개가 모두 통과했습니다. 기존 57개에 경고 재생 순서·사건 연결·취소·재전송·재시작·재생 실패 시 인식 결과 보존 검사 12개를 추가했습니다. 실제 스피커·마이크·ROS2 통신을 사용한 통합 시험은 별도로 필요합니다.
