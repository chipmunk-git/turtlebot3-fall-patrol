# TurtleBot3 실내 순찰 및 낙상 의심 감지 시스템

TurtleBot3 Waffle-Pi가 실내를 순찰하며 낙상 의심 상황을 발견하면 접근·관찰하고, 결과를 웹 대시보드에 기록하여 관리자가 확인하도록 하는 **5인 팀 프로젝트**입니다. ROS2 Humble을 기반으로 자율주행, YOLO 기반 인식, FastAPI 관제 및 Arduino 출력 장치를 연동합니다. 이 시스템의 감지 결과는 **의료적 진단이 아닌 ‘낙상 의심’ 판단**입니다.

현재는 기능별 개발과 통합을 진행 중입니다. **웹 서버의 로봇 상태에 따라 Arduino의 RGB LED·LCD를 변경하는 연동은 임시 개발 환경에서 시험**했으며, 실제 주행·AI 판단부터 모든 장치까지 연결한 최종 실기 시연과 음성 입출력은 별도 검증이 필요합니다.

## 핵심 구성

| 영역 | 주요 기능 | 관련 위치 |
|---|---|---|
| 자율주행·인식 | 실내 순찰, 사람 검출 및 낙상 의심 판단(통합 진행 중) | 팀원별 주행·인식 기능 |
| 관제 서버 | 로봇 상태, 이벤트·사진·활동 기록, 지도·대시보드 | `backend/` |
| ROS2 ↔ 서버 | 로봇 상태·위치 전달, 이벤트 API 호출 | `src/fall_patrol_bridge/` |
| ROS2 ↔ Arduino | USB 시리얼을 통한 LED·LCD·부저 제어 | `src/tb3_arduino_io/`, `arduino/` |
| 서버 상태 → 출력 장치 | 서버 상태를 ROS2의 RGB LED·LCD 명령으로 변환 | `src/dev_scripts/status_to_arduino.py` |

### 상태 연동 흐름

```text
[모의 시연] fake_robot.py ──HTTP──┐
                               ▼
[실기 연동 대상] ROS2 주행·인식 → fall_patrol_bridge → FastAPI / SQLite
                                                       │
                                                       │ GET /status
                                                       ▼
                                            status_to_arduino.py
                                                       │ ROS2 토픽
                                                       ▼
                                             tb3_arduino_io/bridge
                                                       │ USB 시리얼
                                                       ▼
                                             Arduino Uno → RGB LED·LCD
```

`fall_patrol_bridge`는 **ROS2에서 서버로**, `status_to_arduino.py`는 **서버에서 ROS2 출력 장치로** 상태를 연결합니다. `fake_robot.py`는 실제 로봇·AI를 대신하는 모의 시연 프로그램입니다.

## 프로젝트 폴더

```text
project_ws/
├── arduino/tb3_io/tb3_io.ino       # Arduino 펌웨어
├── backend/                        # FastAPI 서버·웹 화면·모의 로봇
├── docs/API.md                     # ROS2·서버 연동 API 안내
├── src/
│   ├── fall_patrol_bridge/         # ROS2 → 서버
│   ├── tb3_arduino_io/             # ROS2 → Arduino 시리얼
│   └── dev_scripts/
│       ├── status_to_arduino.py    # 서버 상태 → ROS2 LED·LCD
│       └── COLCON_IGNORE          # colcon 패키지 탐색 제외
├── build/                          # 빌드 결과 (Git 제외)
├── install/                        # 설치 결과 (Git 제외)
└── log/                            # 실행·빌드 로그 (Git 제외)
```

`build/`, `install/`, `log/`, 가상환경 및 실행 중 생성되는 DB·스냅샷은 Git 관리 대상에서 제외합니다. `src/dev_scripts/`는 독립 실행형 스크립트를 모아 둔 곳으로, ROS2 패키지 자체는 아닙니다.

## 빠른 실행: 웹 상태 → Arduino 표시 장치

아래는 **개발 PC 한 대에서 웹 서버·ROS2·Arduino를 연결하는 통합 시험** 기준입니다. 실제 로봇에서는 Arduino를 Raspberry Pi에 연결하고 브리지를 그쪽에서 실행할 수 있습니다. 이때 웹 서버와 ROS2 노드의 실행 위치 및 주소는 최종 환경에 맞춰 조정합니다.

**실행 전 확인:** Arduino Uno에는 `arduino/tb3_io/tb3_io.ino` 펌웨어를 업로드하고 RGB LED·LCD를 연결해야 합니다. USB로 연결한 Arduino가 Linux에서 `/dev/ttyACM0` 등으로 인식되어야 합니다. 현재 WSL2로 시험한다면 Windows에서 USB 장치를 WSL로 전달한 후 확인하며, 최종 Ubuntu Desktop·Raspberry Pi에서는 실제 USB 연결 상태를 확인합니다. `bridge.py` 실행에는 Python의 `serial` 모듈(pyserial)도 필요합니다.

### WSL2에서 Arduino 연결하기 (임시 시험 환경)

Windows에서 Arduino Uno를 USB로 연결하고, **Arduino IDE에서 `tb3_io.ino` 업로드를 마친 뒤** WSL2에 전달합니다. Windows에는 [`usbipd-win`](https://learn.microsoft.com/ko-kr/windows/wsl/connect-usb)이 설치되어 있어야 하며, WSL Ubuntu 터미널을 열어 둡니다.

**Windows PowerShell(관리자 권한)**에서 장치의 BUSID를 확인하고 공유합니다. 아래 `2-3`은 **예시**이므로 `usbipd list`에 표시된 실제 BUSID로 바꿉니다.

```powershell
usbipd list
usbipd bind --busid 2-3
```

이미 `Shared` 상태라면 `bind`를 다시 실행할 필요가 없습니다. 다음으로 **Windows PowerShell**에서 WSL2에 연결합니다(관리자 권한 불필요).

```powershell
usbipd attach --wsl --busid 2-3
```

**WSL Ubuntu**에서 Arduino 시리얼 장치를 확인합니다.

```bash
lsusb
ls -l /dev/ttyACM*
```

Arduino가 `/dev/ttyACM0` 등으로 나타나면 아래 브리지 실행 단계로 진행합니다. Windows Arduino IDE에서 펌웨어를 다시 업로드하려면 PowerShell에서 `usbipd detach --busid 2-3`으로 장치를 Windows에 반환하고, 업로드를 마친 후 다시 `attach`합니다. USB 분리·재연결이나 재부팅 후에도 `attach`를 다시 해야 할 수 있습니다.

**최초 준비(필요할 때 한 번):** `colcon build`로 ROS2 패키지를 빌드하고, 서버에 필요한 Python 가상환경과 라이브러리를 준비합니다. 이후 실행 터미널에서는 `source install/setup.bash`로 빌드된 ROS2 실행 파일을 사용할 수 있게 합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
colcon build --packages-select tb3_arduino_io fall_patrol_bridge

cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

**터미널 1 — 서버** (`backend/`에서 실행):

```bash
cd ~/robotech/project_ws/backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

대시보드: `http://127.0.0.1:8000` · API 문서: `http://127.0.0.1:8000/docs`

**터미널 2 — Arduino 시리얼 브리지:**

Arduino 펌웨어는 ROS2 토픽을 직접 받지 못합니다. `src/tb3_arduino_io/`의 **`bridge.py`가 ROS2의 LED·LCD·부저 명령을 USB 시리얼 메시지로 변환**해 Arduino에 보냅니다. 아래의 `bridge`는 임의의 파일 이름이 아니라, 이 패키지의 `setup.py`에 등록된 ROS2 실행 명령입니다.

먼저 실제 연결된 장치 이름을 확인합니다.

```bash
ls -l /dev/ttyACM*
```

장치가 `/dev/ttyACM0`으로 인식되었다면 다음과 같이 실행합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run tb3_arduino_io bridge --ros-args -p port:=/dev/ttyACM0
```

- `source install/setup.bash`: 앞서 `colcon build`로 설치한 `tb3_arduino_io` 패키지를 ROS2에서 찾도록 설정합니다.
- `-p port:=/dev/ttyACM0`: 브리지가 연결할 Arduino의 시리얼 장치를 지정합니다. 장치 이름이 다르면 실제 경로로 변경합니다.
- `Waiting for Arduino serial device`와 `Serial connected; waiting for Uno boot`는 연결 과정에서 볼 수 있는 로그입니다. **브리지가 종료되지 않고 실행 상태를 유지하는 것은 정상**입니다.

`serial` 모듈을 찾지 못한다면 사용 중인 Ubuntu 환경에서 `sudo apt install python3-serial`로 설치할 수 있습니다. 장치 파일이 없다면 먼저 USB 연결(WSL2 시험 시 USB 전달 포함)을 확인합니다.

**터미널 3 — 서버 상태 중계:** `status_to_arduino.py`는 FastAPI의 `GET /status`를 약 0.4초마다 조회하고, 상태가 바뀌면 ROS2 LED·LCD 토픽을 발행합니다. 브리지가 이 토픽을 받아 Arduino로 전달합니다.

```bash
cd ~/robotech/project_ws
source /opt/ros/humble/setup.bash
python3 src/dev_scripts/status_to_arduino.py
```

**터미널 4 — 모의 순찰 시연** (`backend/`에서 실행): `fake_robot.py`가 실제 주행·인식 대신 서버 상태와 이벤트를 갱신하므로, 웹 대시보드와 표시 장치가 함께 변하는지 확인할 수 있습니다.

```bash
cd ~/robotech/project_ws/backend
.venv/bin/python -m scripts.fake_robot --observe 8
```

서버 화면과 Arduino를 함께 보면서 상태 변화를 확인합니다. 서버 연결이 다른 PC를 가리킨다면 `status_to_arduino.py --server-url http://서버주소:8000`으로 지정합니다. **모의 시연 중에는 실제 `status_bridge` 등 다른 상태 전송 프로그램이 동시에 `/status`를 갱신하지 않도록** 합니다.

## Arduino 출력 장치

| 서버 상태 | LED | LCD (1행 / 2행) |
|---|---|---|
| `idle` | 꺼짐 | IDLE / STANDBY |
| `patrol` | 초록 | MONITORING / PATROL ACTIVE |
| `suspect` | 노랑 | PERSON DETECTED / CHECKING |
| `approach` | 파랑 | APPROACHING / KEEP DISTANCE |
| `observe` | 노랑 | OBSERVING / PLEASE WAIT |
| `alert` | 빨강 | FALL SUSPECTED / STAFF CHECK |

Arduino Uno에는 RGB LED(D5·D6·D9), 부저(D8), 1602 I2C LCD(SDA=A4, SCL=A5)를 연결합니다. `bridge`는 `/tb3/rgb_led`, `/tb3/lcd_text`, `/tb3/lcd_power`, `/tb3/buzzer`를 구독합니다.

부저 펌웨어는 켜기 명령 후 **0.15초 울림 → 0.6초 쉼**을 반복하고 끄기 명령을 받으면 멈추도록 작성되어 있습니다. 단, **현재 `status_to_arduino.py`는 부저를 자동 제어하지 않습니다.** `observe`·`alert`의 음성 안내도 실제 재생이 아닌 터미널의 `[가상 스피커]` 출력입니다.

## 진행 상황과 다음 단계

- **확인:** ROS2에서 Arduino의 LED·LCD·부저 제어, 부저 간헐음 시험, 웹 서버 상태에 따른 LED·LCD 연동 시험(임시 개발 환경).
- **코드 제공·별도 통합 필요:** `fall_patrol_bridge`의 실기 연동, 실제 주행·낙상 인식 결과를 통한 서버 상태·이벤트 처리.
- **예정:** Arduino의 Raspberry Pi 장착·전원/USB 허브 검증, 마이크·스피커 수령 후 음성 안내·Whisper 시험, 재연결 시 표시 상태 동기화 보완.

최종 운영에서는 WSL2가 아닌 **Ubuntu Desktop 원격 PC와 TurtleBot3의 Raspberry Pi**를 사용합니다. 실제 장치가 분리되면 ROS2 노드의 실행 위치, 서버 주소 및 네트워크 설정을 해당 환경에 맞춰 확인해야 합니다.

## 상세 문서

- **[팀원 GUIDE.md](https://github.com/GGo-ri/project_3/blob/main/GUIDE.md)** — 백엔드·프런트엔드 설계, 감지 시나리오, 이벤트 상태, 무시 구역, DB, API 및 서버 운영
- **[docs/API.md](docs/API.md)** — ROS2 주행·인식 기능과 서버의 API 연동 방법
- **[src/README.md](src/README.md)** — `fall_patrol_bridge` 사용 방법과 ROS2 상태 토픽 안내 (일부 실행 경로는 과거 작업공간 기준이므로 현재 구조를 우선)

팀원이 작성한 서버·웹 기능은 기존 API를 우선 활용하고, 변경이 필요한 경우 담당자와 협의하여 통합합니다.
