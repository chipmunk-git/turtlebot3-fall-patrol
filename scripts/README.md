# 한 명령으로 실기 프로그램 실행

Ubuntu 서버 PC에서 [인수인계 문서](../docs/HANDOVER.md)의 1~10번 순서대로 시작합니다. Pi 프로그램은 SSH로 실행하며 기능별 로그를 tmux 창에서 봅니다.

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

SSH·tmux 서버·ROS2·로봇·음성 장치를 사용하지 않고 순서, 인자, 준비 실패, 로그 연결과 종료 순서를 검사합니다. 실제 두 컴퓨터에서의 SSH 인증·장치 연결·시리얼 출력·RViz·ROS2 통신과 전체 시작·종료는 실기 확인이 필요합니다.
