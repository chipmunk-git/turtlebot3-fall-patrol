# 인수인계: 실기 실행과 남은 확인

TurtleBot3 낙상 의심 순찰 시스템을 실제 로봇에서 실행하는 순서와, 2026-11-03 실기 점검에서 확인한 내용입니다. 세부 동작은 [최상위 README](../README.md)와 [순찰·음성 안내](../src/my_bot_fall/README.md)를 참고합니다.

| 표기 | 의미 |
|---|---|
| `ROBOT_IP` | Raspberry Pi 주소 |
| `SERVER_IP` | Ubuntu 서버 PC 주소 |
| `~/robotech/project_ws` | PC의 저장소 경로 (다른 곳에 받았다면 그 경로) |
| `~/fall_patrol/project_ws` | Pi의 작업공간 |

## 구성

| 위치 | 실행 항목 |
|---|---|
| Pi | bringup, 카메라, NUCLEO 브리지, 음성 재생·녹음 |
| PC | FastAPI 서버, Whisper 인식, RViz·지도·AMCL, 영상 서버, 자세 판정, 순찰, LED·LCD 상태 전송 |

STM32 NUCLEO는 터틀봇(Pi)에 USB로 연결합니다. 브리지는 USB가 연결된 Pi에서, `status_to_arduino.py`는 PC에서 실행하며 둘은 ROS2 토픽으로 통신합니다.

## 한 명령으로 시작

서버 PC에서 `bash scripts/demo.sh start --robot ubuntu@ROBOT_IP --server-url http://SERVER_IP:8000`으로 아래 1~10번 순서를 실행할 수 있습니다. 준비 확인·기능별 로그·종료 방법은 [자동 실행 안내](../scripts/README.md)를 참고합니다. RViz 초기 위치·순찰 지점 지정과 웹의 주행 시작은 기존 절차를 따릅니다.

## 처음 한 번 준비

**PC**

```bash
sudo apt install ros-humble-web-video-server
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

음성 인식에 필요한 `whisper.cpp`를 준비합니다. 기본 경로는 `~/whisper.cpp/build/bin/whisper-cli`, `~/whisper.cpp/models/ggml-base-q8_0.bin`입니다.

```bash
cd ~
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build
cmake --build build --config Release
sh models/download-ggml-model.sh base-q8_0
```

**Pi 코드 배치** (PC에서 실행)

```bash
cd ~/robotech/project_ws
rsync -avR src/my_bot_fall src/tb3_arduino_io voice/stt/whisper_session.py voice/stt/response_classifier.py voice/tts/observe.mp3 voice/tts/alert.mp3 ubuntu@ROBOT_IP:fall_patrol/project_ws/
```

**Pi 빌드**

```bash
cd ~/fall_patrol/project_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select my_bot_fall tb3_arduino_io
```

## 고정 장치 이름

`/dev/ttyACM0` 같은 번호는 연결 순서에 따라 바뀝니다. 실기 점검에서 NUCLEO가 `ttyACM0`, OpenCR이 `ttyACM1`로 잡혀 `robot.launch.py` 기본값으로는 `turtlebot3_node`가 뜨지 않았습니다. 번호 대신 장치 이름을 사용합니다.

Pi에서 확인합니다.

```bash
ls -l /dev/serial/by-id/
arecord -l
aplay -l
```

| 장치 | 지정 방법 |
|---|---|
| OpenCR | `$(ls /dev/serial/by-id/usb-ROBOTIS_OpenCR*)` |
| NUCLEO | `$(ls /dev/serial/by-id/usb-STMicroelectronics_STM32_STLink*)` |
| 마이크 | `plughw:CARD=microphone,DEV=0` |
| 스피커 | `plughw:CARD=UACDemoV10,DEV=0` |

`CARD=` 값은 `arecord -l`, `aplay -l`에서 `card N:` 바로 뒤의 이름입니다. 다른 장치를 쓰면 이름을 바꿉니다. 같은 종류의 장치를 두 개 연결하면 이 이름만으로는 구분되지 않을 수 있습니다.

## 실행 순서

명령마다 새 터미널을 엽니다. 한 터미널에서 다른 작업공간의 `install/setup.bash`를 함께 불러오지 않습니다.

**1. Pi: bringup**

```bash
ros2 launch turtlebot3_bringup robot.launch.py usb_port:=$(ls /dev/serial/by-id/usb-ROBOTIS_OpenCR*)
```

PC에서 `ros2 topic info /cmd_vel`의 `Subscription count`가 1이고 `ros2 topic hz /odom`에 주기가 나오는지 확인합니다.

**2. Pi: 카메라**

```bash
ros2 run v4l2_camera v4l2_camera_node --ros-args -p image_size:="[320,240]" -p time_per_frame:="[1,15]" -p image_raw.jpeg_quality:=50
```

**3. PC: 서버**

```bash
cd ~/robotech/project_ws/backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**4. Pi: NUCLEO 브리지** (LED·LCD를 쓸 때)

```bash
cd ~/fall_patrol/project_ws
source install/setup.bash
ros2 run tb3_arduino_io bridge --ros-args -p port:=$(ls /dev/serial/by-id/usb-STMicroelectronics_STM32_STLink*)
```

**5. Pi: 음성** (음성을 쓸 때)

```bash
cd ~/fall_patrol/project_ws
source install/setup.bash
amixer -c UACDemoV10 sset PCM 60%
ros2 run my_bot_fall voice --ros-args -p script_path:="$(pwd)/voice/stt/whisper_session.py" -p server_url:=http://SERVER_IP:8000 -p input_device:=plughw:CARD=microphone,DEV=0 -p output_device:=plughw:CARD=UACDemoV10,DEV=0
```

`server_url`을 빼면 Pi 안에서 인식하는 시험 모드가 됩니다. 항상 지정합니다.

**6. PC: LED·LCD 상태 전송** (LED·LCD를 쓸 때)

```bash
cd ~/robotech/project_ws
source install/setup.bash
python3 src/dev_scripts/status_to_arduino.py --server-url http://127.0.0.1:8000
```

**7. PC: 지도, AMCL, RViz**

```bash
cd ~/robotech/project_ws
source install/setup.bash
ros2 launch my_bot_fall tb3_localization_real.launch.py map:="$PWD/src/maps/new_map.yaml"
```

**8. PC: 대시보드 영상**

```bash
source /opt/ros/humble/setup.bash
ros2 run web_video_server web_video_server
```

**9. PC: 자세 판정**

```bash
cd ~/robotech/project_ws
source install/setup.bash
ros2 run my_bot_fall lying_detector --ros-args --params-file "$(ros2 pkg prefix my_bot_fall)/share/my_bot_fall/config/params_robot.yaml"
```

**10. PC: 순찰** (가장 마지막, 같은 노드를 두 개 실행하지 않습니다)

```bash
cd ~/robotech/project_ws
source install/setup.bash
ros2 run my_bot_fall patrol --ros-args -p server_url:=http://127.0.0.1:8000 -p voice_enabled:=true -p waypoint_count:=3 -p linear_speed:=0.15
```

5번을 실행하지 않으면 `voice_enabled:=false`로 바꿉니다.

## 주행 시작과 정지

1. 대시보드 `http://SERVER_IP:8000`을 엽니다.
2. RViz **2D Pose Estimate**로 실제 위치와 방향을 지정합니다. 레이저 점이 지도 벽과 겹치는지 확인합니다.
3. RViz **2D Goal Pose**로 순찰 지점 3곳을 지정합니다. 지점 간격은 25cm 이상이며, 지정한 순서대로 반복합니다. 지점 사이 경로는 A*로 계산합니다.
4. 순찰 터미널에 `목적지 설정 완료`가 나오면 대시보드에서 **주행 시작**을 누릅니다.

**정지:** 대시보드 **정지**를 누릅니다. 급하면 순찰 터미널에서 Ctrl+C를 누르고, 그래도 움직이면 본체 전원을 끕니다.

## 2026-11-03 실기 확인 결과

| 항목 | 결과 |
|---|---|
| 3개 지점 순찰, 웹 시작·정지 | 로그에서 목표 도착과 웹 정지 확인 |
| Pi 재생 → Pi 녹음 → PC 인식 → 사건 화면 API | `whisper_session.py` 단독 실행으로 확인, "도와주세요" → `도움 필요` |
| NUCLEO 브리지와 LED 상태 표시 | LED가 로봇 상태에 따라 바뀌는 것 확인 |
| 마이크, 스피커 카드 이름 | 녹음·재생 명령 동작 확인 |

## 아직 확인하지 못한 것

- 이 문서의 `$(ls /dev/serial/by-id/...)` 형태로 실행한 bringup과 브리지. 문제가 있으면 `ls -l /dev/serial/by-id/`로 경로나 번호를 확인해 직접 지정합니다.
- 순찰 중 낙상 관찰에서 음성이 자동으로 시작되어 사건에 연결되는 전체 흐름.
- 1~2m 거리, 소음, 다른 사람 목소리에서의 음성 인식. 등록 표현은 "도와주세요"(`도움 필요`), "괜찮아요"(`도움 불필요`)이며 "도와줘", "아파요" 등은 `확인 필요`입니다.
- JPEG 화질 50이 자세 판정 정확도에 주는 영향. 네트워크 문제를 조사하며 낮춘 값입니다.

## 미해결 문제와 단서

**네트워크가 간헐적으로 느려집니다.** 라이다·영상이 0~3Hz로 떨어지고 ping이 수백 ms로 늘어나며, IP 조각 재조립 실패가 급증합니다. 이때 AMCL이 멈춰 `map` 좌표계가 생기지 않을 수 있습니다.

- 정상 때 기준: `/odom` 20Hz, `/scan` 약 10Hz, ping 수 ms.
- 원인은 확정하지 못했습니다. 카메라 데이터량을 약 70% 줄여도 나아지지 않았고, 신호 세기는 보통(-59 dBm), Pi 저전압 기록은 없었습니다.
- Pi 와이파이 절전 모드가 켜져 있었습니다. 유력한 원인이지만 부하 상태에서의 효과는 확인하지 못했습니다. 아래 설정은 재부팅하면 되돌아갑니다.

```bash
sudo iw dev wlan0 set power_save off
iw dev wlan0 get power_save
```

**위치를 지정해도 `AMCL 위치 수신 대기`가 계속되는 경우:** AMCL 로그에 `Failed to transform initial pose`가 나오고 TF 시각이 시작 직후에 멈춰 있으면, 네트워크가 정상인지 확인한 뒤 7번 launch를 다시 실행합니다. `ros2 run tf2_ros tf2_echo map base_footprint`에 값이 나오면 적용된 것입니다.

## 자주 만나는 증상

| 증상 | 확인할 것 |
|---|---|
| `/odom`, `/cmd_vel`이 없음 | OpenCR 전원과 USB, bringup의 `usb_port` |
| 대시보드 `연결 끊김` | `patrol`이 실행 중인지 (서버 상태를 갱신하는 노드) |
| 주행 시작이 정지로 돌아감 | 지점 3곳 지정 여부, 순찰 터미널의 거부 메시지 |
| LED 대기 녹색, 순찰 파랑, 관찰 보라 | 초록·파랑 선이 바뀐 상태. NUCLEO D6이 초록, D10이 파랑, D7이 빨강 |
| 음성 인식 문장이 엉뚱함 | 녹음 음량. `alsamixer -c microphone`에서 입력을 올리고 안내 음성이 끝난 뒤 말하기 |
| 스피커 100%에서 `mpg123` 오류 | 60% 정도로 낮춰서 재생 |
| `ros2 has stopped unexpectedly` 창 | `/var/crash`의 대상 확인. RViz는 종료 때 `exit code -11`이 반복되며 동작에는 영향이 없었음 |

## 배터리

전압이 11V에 가까워지면 충전합니다. 오래 쉴 때는 Pi를 `sudo shutdown now`로 끄고 본체 전원을 끕니다.
