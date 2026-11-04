# 한 명령으로 실기 프로그램 실행

Ubuntu 서버 PC에서 [인수인계 문서](../docs/HANDOVER.md)의 1~10번 순서대로 시작합니다. Pi 프로그램은 SSH로 실행하며 기능별 로그를 tmux 창에서 봅니다.

## 명령어 모음

모두 `bash scripts/demo.sh <명령>` 형태로 저장소 최상위 폴더에서 실행합니다.

| 하고 싶은 일 | 명령 |
|---|---|
| 서버까지 전부 시작, 종료할 때 서버 유지 | `start --external-server --robot ubuntu@ROBOT_IP --server-url http://SERVER_IP:8000` |
| 서버까지 전부 시작, 종료할 때 서버도 함께 종료 | `start --robot ubuntu@ROBOT_IP --server-url http://SERVER_IP:8000` |
| 순찰 경로 다시 지정 (순찰만 재시작) | `patrol-restart` |
| 로봇 프로그램 종료 | `stop` |
| 프로그램별 실행 상태 확인 | `status` |
| 실행 화면 열기 | `attach` |
| 서버만 시작 | `server-start` |
| 서버만 종료 (로봇 프로그램이 켜져 있으면 거부) | `server-stop` |
| 서버 상태와 실행 폴더 확인 | `server-status` |
| 서버만 재시작 | `server-stop` 후 `server-start` |
| 실행할 명령만 확인 | `plan --robot ubuntu@ROBOT_IP --server-url http://SERVER_IP:8000` |

자세한 내용은 아래 [서버를 유지하며 실행](#서버를-유지하며-실행)과 [순찰 경로 다시 지정](#순찰-경로-다시-지정)을 참고합니다.

## 준비

PC와 Pi에는 인수인계 문서의 코드·빌드 결과·ROS2 환경·장치가 준비되어 있어야 합니다. Pi의 기본 작업공간은 `~/fall_patrol/project_ws`입니다. PC에서는 저장소 안의 스크립트 위치를 기준으로 작업공간을 찾습니다.

서버 PC에 tmux·SSH 클라이언트·curl이 없다면 설치합니다.

```bash
sudo apt install tmux openssh-client curl
```

Pi에도 `curl`, `mpg123`, `arecord`, `amixer`가 필요합니다. 기존 Whisper 실행 파일·모델은 서버 PC에서 사용합니다. 기본 위치는 `~/whisper.cpp/build/bin/whisper-cli`와 `~/whisper.cpp/models/ggml-base-q8_0.bin`이며, 다른 위치라면 기존 환경 변수 `FALL_WHISPER_CLI`·`FALL_WHISPER_MODEL`을 지정합니다.

RViz를 표시할 Ubuntu 데스크톱 터미널에서 실행합니다. 수동으로 실행했던 순찰·teleop·카메라·bringup·지도·영상 서버·음성·브리지·FastAPI는 먼저 종료합니다. 스크립트가 기존 노드, `/cmd_vel` 발행자, 사용 중인 서버 포트를 발견하면 시작을 중단합니다.

## 시작

접속 대상 `ubuntu@ROBOT_IP`와 Pi에서 접근할 수 있는 `SERVER_IP`를 실제 값으로 바꿉니다.

```bash
bash scripts/demo.sh start \
  --robot ubuntu@ROBOT_IP \
  --server-url http://SERVER_IP:8000
```

처음에 SSH 호스트 확인과 인증을 진행합니다. 단계마다 센서 수신·서버 응답·노드와 시리얼 연결 상태를 확인합니다. FastAPI는 음성 동시 실행 제한에 맞춰 `--workers 1`로 실행합니다.

단계마다 기본 60초 동안 준비를 확인합니다. 시작한 프로그램이 먼저 종료되거나 준비 확인에 실패하면 다음 단계로 넘어가지 않고, 이 실행에서 시작한 프로그램을 종료 순서에 따라 정리합니다. 오류와 기능별 로그를 확인한 뒤 다시 시작합니다. 준비 시간을 늘리려면 `--timeout 120`을 지정합니다.

준비가 끝나면 tmux의 순찰 창이 열립니다. RViz에서 **2D Pose Estimate**로 실제 위치·방향을 지정하고 **2D Goal Pose**로 순찰 지점 3곳을 지정한 다음, 웹에서 **주행 시작**을 누릅니다. 위치 추정 준비 확인은 초기 위치를 지정하기 전의 지도·AMCL 활성화까지 확인합니다.

## 서버를 유지하며 실행

로봇을 꺼도 웹 대시보드와 사건 기록을 계속 볼 수 있습니다. `--external-server`를 붙이면 명령 하나로 서버 → 로봇 프로그램 8개 → 순찰을 차례로 시작합니다. 옵션 없이 `start`를 실행하면 기존처럼 서버까지 함께 시작하고 종료합니다.

```bash
# 시작 (서버가 꺼져 있으면 먼저 켜고, 켜져 있으면 그대로 사용)
bash scripts/demo.sh start --external-server \
  --robot ubuntu@ROBOT_IP \
  --server-url http://SERVER_IP:8000

# 로봇 프로그램만 종료 (주행 정지 명령은 그대로 보내고 서버는 유지)
bash scripts/demo.sh stop

# 서버만 따로 다루기
bash scripts/demo.sh server-start
bash scripts/demo.sh server-status
bash scripts/demo.sh server-stop
```

`--external-server`는 8000번 서버가 응답하는지와 **이 작업공간의 `backend`에서 실행된 서버인지** 함께 확인합니다. 다른 폴더의 서버가 8000번을 쓰고 있으면 시작을 중단합니다. 시작 과정에서 서버가 사라져도 중단합니다.

서버 로그는 `log/demo/server.log`에 쌓입니다. 화면으로 보려면 `tmux -L turtlewatch-server attach`를 실행합니다. `server-stop`은 로봇 프로그램이 실행 중이면 거부하고, `server-start`로 시작하지 않은 서버는 종료하지 않습니다.

터미널에서 서버를 직접 실행해도 이 작업공간의 서버면 `--external-server`가 그대로 사용합니다. 이 경우 종료는 그 터미널에서 Ctrl+C로 합니다.

## 순찰 경로 다시 지정

순찰 지점을 다 찍은 뒤에는 정지를 눌러도 지점이 유지되므로, 경로를 바꾸려면 순찰 프로그램만 재시작합니다.

```bash
bash scripts/demo.sh patrol-restart
```

웹에 정지 명령을 보내고 순찰을 종료한 뒤 같은 tmux 창에서 다시 시작해 준비를 확인합니다. 나머지 프로그램은 계속 실행됩니다. 이후 RViz **2D Goal Pose**로 지점을 지정하고 웹에서 **주행 시작**을 누릅니다. 주행은 자동으로 시작하지 않습니다.

처리하지 않은 사건이 있으면 경고 상태로 복구되므로 웹에서 먼저 처리해야 지점을 지정할 수 있습니다. 재시작 후 위치를 받지 못했다는 경고가 나오면 **2D Pose Estimate**를 다시 지정합니다.

## 로그 화면

서버 PC의 tmux 창 순서는 다음과 같습니다.

| 창 번호 | 내용 |
|---|---|
| 1 | 순찰 |
| 2 | 자세·낙상 의심 판정 |
| 3 | Pi 음성 |
| 4 | FastAPI |
| 5 | Pi NUCLEO 브리지 |
| 6 | 출력 중계 |
| 7 | 지도·AMCL·RViz |
| 8 | 영상 서버 |
| 9 | Pi bringup |
| 10 | Pi 카메라 |

`Ctrl+B` 다음 `w`로 창 목록을 엽니다. `Ctrl+B` 다음 `d`로 화면에서 빠져나오면 프로그램은 계속 실행됩니다.

```bash
bash scripts/demo.sh attach
bash scripts/demo.sh status
```

각 창의 출력은 `log/demo/실행시각/` 아래 `patrol.log`, `detector.log` 등으로 보관합니다. `status`에서 프로세스 상태와 로그 폴더를 확인합니다.

tmux 화면을 빠져나온 뒤에도 PC와 Pi 사이의 SSH 연결이 프로그램을 연결합니다. 네트워크 단절·PC 종료 중의 실제 정지와 복구는 장비에서 확인해야 합니다.

## 종료

서버 PC의 별도 터미널에서 실행합니다.

```bash
bash scripts/demo.sh stop
```

웹에 정지 명령을 보내고, 순찰 → 음성 → 출력 중계 → NUCLEO 브리지 → 자세 판정 → 위치 추정 → 영상 서버 → 카메라 → bringup → FastAPI 순서로 Ctrl+C를 전달합니다.

순찰의 종료가 확인되지 않으면 통신을 유지하고 오류를 표시합니다. 출력 중계가 15초 안에 종료되지 않아도 브리지·센서·서버와 SSH 연결을 유지하고 오류를 표시합니다. 해당 tmux 창과 로봇 상태를 확인해 종료 지연 원인을 해결한 뒤 `bash scripts/demo.sh stop`을 다시 실행합니다.

## 장치·경로 변경과 실행 계획 확인

OpenCR·NUCLEO는 `/dev/serial/by-id/`에서 일치하는 장치를 하나씩 찾습니다. 여러 개가 일치하거나 권한이 없으면 중단합니다. 포트와 오디오 장치·Pi 경로가 다르면 다음 옵션을 시작 명령에 추가합니다.

```text
--pi-root ~/fall_patrol/project_ws
--opencr-port /dev/serial/by-id/OPENCR_DEVICE
--mcu-port /dev/serial/by-id/NUCLEO_DEVICE
--input-device plughw:CARD=microphone,DEV=0
--output-device plughw:CARD=UACDemoV10,DEV=0
--speaker-card UACDemoV10
--domain 33
```

`--pi-root`에 Pi의 홈 기준 경로를 줄 때는 `'~/fall_patrol/project_ws'`처럼 작은따옴표로 감싸 PC의 홈으로 미리 확장되지 않도록 합니다. ROS 도메인은 기본적으로 PC의 `ROS_DOMAIN_ID`를 사용하고, 설정되어 있지 않으면 기존 실기 값 33을 사용합니다. Pi 프로그램에도 같은 값을 전달합니다.

실제 연결 전에 실행할 명령만 확인할 수 있습니다.

```bash
bash scripts/demo.sh plan --robot ubuntu@ROBOT_IP --server-url http://SERVER_IP:8000
```

## 모의 검사

```bash
backend/.venv/bin/python -B -m unittest discover -s scripts -p 'test_demo_launcher.py' -v
```

SSH·tmux 서버·ROS2·로봇·음성 장치를 사용하지 않고 순서, 인자, 준비 실패, 로그 연결과 종료 순서, 외부 서버 확인을 검사합니다. 실제 두 컴퓨터에서의 SSH 인증·장치 연결·시리얼 출력·RViz·ROS2 통신과 전체 시작·종료는 실기 확인이 필요합니다.
