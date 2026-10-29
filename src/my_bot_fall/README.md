# 순찰·자세 판정과 Raspberry Pi 음성 연결

`lying_detector`가 자세 결과를 `/pose_state`에 발행하고, `patrol`이 순찰·현위치 정지 관찰·경고·웹 확인 후 재개를 관리합니다. 의심 대상에게 추가 접근하지 않습니다. 자세 모델은 `lying_detector`에서 한 개만 실행합니다.

`patrol`이 `/status`와 `/events`를 직접 갱신합니다. 같은 서버에 `status_bridge`나 `fake_robot`을 동시에 실행하지 않습니다. `/cmd_vel`을 발행하는 기존 `navigation`, 다른 주행 프로그램, teleop, Nav2 controller도 동시에 실행하지 않습니다.

## 연결 구조

```text
Ubuntu ROS2 실행 PC                         Raspberry Pi 4B
lying_detector → /pose_state → patrol
                                │ ── /fall_patrol/voice_request ──→ voice
                                │                                  │
                                │                 observe.mp3 재생 완료
                                │                       → 5초 녹음
                                │                       → whisper.cpp base-q8_0
                                │                       → 등록 표현 분류
                                │ ←─ /fall_patrol/voice_result ─────┘
                                │
                           기존 사건 메모 PATCH
                                ▼
                         FastAPI / 대시보드
```

주행·자세 판정 노드는 기존 원격 Ubuntu PC 실행 방식을 사용할 수 있습니다. Pi는 기존 로봇 bringup·센서 송신과 음성 입출력을 맡습니다. 음성 연결 때문에 주행·추론 전체를 Pi로 이전할 필요는 없습니다.

관찰별 식별자로 음성을 한 번 실행합니다. 요청·결과가 유실돼 재전송하더라도 완료 결과를 다시 전달합니다. Pi 기록은 기본 `~/.turtlewatch/voice_requests.json`에 보관하며, 중간에 재시작한 교환은 오류로 반환하고 자동 재생하지 않습니다. **Pi 음성 노드는 한 개만 실행합니다.**

음성 결과는 기존 사건의 **메모에만** 기록합니다. 도움 요청·거절·확인 필요·실패 모두 영상 판정이나 웹의 상태 처리를 자동으로 뒤집지 않습니다. 음성 때문에 새 사건을 만들거나 자동으로 순찰을 재개하지 않습니다.

기존 사건 생성 시점인 `alert`는 유지합니다. 음성 결과가 먼저 오면 같은 관찰에 보관한 뒤 생성된 `event_id`에 연결합니다. 사건 생성 전에 정상 자세로 순찰에 복귀하거나 웹 확인으로 재개 대기에 들어가면 미완료 음성을 취소합니다. 이전 관찰의 결과는 새 사건에 반영하지 않습니다.

## 실행 전 조건

- ROS2 Humble과 기존 주행·자세 추론 의존성, bringup·카메라·지도·AMCL 환경을 준비합니다. 환경을 자동 설치하거나 변경하지 않습니다.
- Pi와 ROS2 실행 PC는 같은 ROS2 도메인과 통신 가능한 네트워크를 사용하고 시계를 동기화합니다. 음성 요청은 만료 시각을 검사합니다.
- Pi에 기존 `whisper-cli`, `ggml-base-q8_0.bin`, `mpg123`, `arecord`가 있어야 합니다. 기본 Whisper 위치는 `~/whisper.cpp/`입니다.
- `arecord -l`, `aplay -l`로 실제 ALSA 장치를 확인합니다. 다른 컴퓨터의 카드 번호를 그대로 사용하지 않습니다.

서버·Pi 경로는 [최상위 README](../../README.md#실행-환경과-작업-경로)를 따릅니다. **Pi 목표 경로에 아래 파일을 배치하고 실행하는 검증은 아직 완료하지 않았습니다.** Pi에는 `my_bot_fall` 패키지, `voice/stt/whisper_session.py`, `voice/stt/response_classifier.py`, `voice/tts/observe.mp3`, Whisper 실행 파일·모델이 필요합니다. 저장소 전체 배포가 필수인 것은 아닙니다.

## Ubuntu PC: 빌드·서버·순찰

필요한 소스가 작업공간에 배치된 경우 빌드합니다. `--base-paths src`로 `parts/`의 같은 이름 패키지가 함께 탐색되지 않도록 합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select my_bot_fall
source install/setup.bash
```

FastAPI가 실행 중이지 않을 때 서버 터미널에서 실행합니다.

```bash
cd ~/robotech/project_ws/backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

기존 bringup·카메라·지도·AMCL 명령을 적용하고 RViz에서 실제 초기 위치를 맞춘 뒤, 자세 노드 터미널에서 실행합니다.

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

RViz에서 A/B/C 지점을 지정하면 기존 안전 조건이 충족될 때 반복 순찰합니다. 관찰 제한은 기본 15초이며 자세 판정·영상 중단 시 경고와 오탐 후 재개 조건은 기존 구현을 사용합니다. 음성만 제외하려면 `-p voice_enabled:=false`로 실행합니다. 음성 결과 대기 기본값은 90초이며 관찰 제한과 별개입니다.

## Raspberry Pi: 음성 실행

다음은 목표 작업공간에 필요한 파일을 배치하고 기존 ALSA·Whisper 환경을 준비한 뒤 확인할 명령입니다.

```bash
cd ~/fall_patrol/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select my_bot_fall
source install/setup.bash
ros2 run my_bot_fall voice --ros-args -p script_path:="$(pwd)/voice/stt/whisper_session.py"
```

기본 녹음·재생 장치는 ALSA `default`입니다. 필요하면 실제 장치를 `-p input_device:=...`, `-p output_device:=...`로 지정합니다. 모델은 `-p model_path:=...`, Whisper 실행 파일은 `-p whisper_cli:=...`, Python은 `-p python_executable:=...`로 지정할 수 있습니다.

ROS2 없이 Pi 음성 교환만 수동 확인할 때는 다음 명령을 사용합니다. **실제로 음성이 재생되고 마이크로 녹음합니다.** 자동 음성 노드와 동시에 실행하지 않습니다.

```bash
cd ~/fall_patrol/project_ws
python3 voice/stt/whisper_session.py
```

성공하면 `status`, `text`, `classification`, `transcribe_seconds` 등을 JSON으로 출력하고, 실패하면 단계와 오류를 반환합니다. WAV는 임시 파일로 만들고 종료 시 제거합니다. 기본값은 디지털 무음만 거르며 일반적인 무음·작은 목소리·Whisper의 잘못된 문장 생성 문제를 해결한 것은 아닙니다. `speech_validated: false`이며 음성만으로 도움 거절을 확정하지 않습니다.

## 서버 PC: 기존 출력 장치 연결

USB 장치·펌웨어를 확인한 뒤 브리지 터미널에서 실행합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select tb3_arduino_io
source install/setup.bash
ros2 run tb3_arduino_io bridge --ros-args -p port:=/dev/ttyACM0
```

상태 조회 프로그램 터미널에서 실행합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
python3 src/dev_scripts/status_to_arduino.py --server-url http://127.0.0.1:8000
```

`/dev/ttyACM0`은 예시입니다. 브리지는 115200 baud, 줄바꿈으로 끝나는 `R,r,g,b`, `L,첫째줄|둘째줄`, `P,0/1`, `B,0/1`, `H`를 보냅니다. LCD는 표시 가능한 ASCII만 사용합니다. 확인한 펌웨어는 **Arduino Uno용**이며 STM32 NUCLEO-F103RB 펌웨어는 찾지 못했습니다. 실제 STM32 코드가 이 규약을 지원하는지 확인하기 전에는 호환·통합 성공을 보장할 수 없습니다.

상태 조회 프로그램은 RGB·LCD 문구·LCD 전원을 자동 갱신합니다. 로봇 표시는 대기(`idle`, 파랑)·순찰(`patrol`, 초록)·관찰(`observe`, 노랑)·알림(`alert`, 빨강) 네 가지입니다. 이전 `suspect`·`approach`는 표시에서만 관찰로 묶으며 서버 값·기록·주행 동작은 바꾸지 않습니다. 사건 처리 상태는 별도로 유지합니다. 기존 LCD 문구와 부저 동작도 유지하며, 부저 토픽은 브리지가 지원하지만 상태 조회 프로그램에서 자동 발행하지 않습니다. 종료 시에는 대기 색상이 아닌 LED 꺼짐으로 마무리합니다.

## 남은 실기 확인

- ROS2에서 사용하는 `parts/my_ws/new_map.yaml` 원점은 `[-3.94, -1.06, 0]`, 서버 지도는 `[-3.39, -0.91, 0]`입니다. 같은 해상도라도 좌표가 일치한다고 볼 수 없습니다. 지도와 서버 파일은 자동 교체하지 않았으며, 웹 위치 표시 전에 실제 사용 지도를 맞춰야 합니다.
- 기존 `location_ok()`는 AMCL 위치 보유 여부만 검사합니다. 오래된 위치·TF 처리와 실제 센서 중단 시 정지는 실기 확인이 필요합니다. 음성 연결을 이유로 위치 판정·주행 규칙을 교체하지 않았습니다.
- ROS2 요청 수신, 실제 스피커→마이크 순서, 작은 목소리·무음, 추론 시간, 같은 사건 메모·사진 및 사람의 확인 후 재개를 장비에서 확인해야 합니다.
- 메모는 GET 후 기존 내용에 덧붙여 PATCH하고 식별자로 재전송 중복을 막습니다. 서버에 조건부 갱신 기능이 없어 동시 웹 메모 편집과의 경쟁은 남습니다.
- `alert.mp3` 자동 재생과 음성에 따른 사건 상태 자동 변경은 추가하지 않았습니다.

## 장비 없는 모의 테스트

개인 개발 환경의 프로젝트 루트에서 실행합니다.

```bash
backend/.venv/bin/python -B -m unittest discover -s src/my_bot_fall/tests -p 'test_*.py' -v
backend/.venv/bin/python -B -m unittest discover -s voice -p 'test_*.py' -v
backend/.venv/bin/python -B -m unittest discover -s voice/stt -p 'test_*.py' -v
```

2026-10-29 사용자 터미널에서 순찰 패키지 38개, 기존 Python 음성 23개·STT 18개, 신규 Pi 음성 교환 8개를 분할 실행해 총 87개가 통과했습니다. 기존 순찰·안전 정지·오탐 처리와 음성 중복·취소·재시작·제한 시간, 같은 이벤트 메모 반영, 재생·녹음 실패·무음·취소 시 WAV 제거를 모의 검증합니다. ROS2 통신·모델 추론·하드웨어 통합 성공을 의미하지 않습니다.
