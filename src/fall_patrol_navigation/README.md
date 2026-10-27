# A* 주행 노드

지도·AMCL 위치·LiDAR·카메라를 사용해 RViz에서 지정한 단일 목표점까지 주행합니다. 가감속 제한과 영상 처리 분리를 적용하며, 유효한 위치 TF가 없으면 정지합니다.

ROS2 Humble과 `cv_bridge`, `tf2_ros`, NumPy, OpenCV, Ultralytics·PyTorch가 준비된 Ubuntu 환경에서 실행합니다.

## 빌드와 실행

아래 명령은 ROS2와 로봇의 센서 토픽·TF가 준비된 컴퓨터의 프로젝트 루트에서 실행합니다. 서버·라즈베리파이의 작업 경로는 [프로젝트 README](../../README.md#실행-환경과-작업-경로)를 따르며, 개인 개발 경로는 달라도 됩니다.

```bash
source /opt/ros/humble/setup.bash
colcon build --base-paths src --packages-select fall_patrol_navigation
source install/setup.bash
```

로봇 bringup, 지도·AMCL 위치 추정, 카메라가 준비된 상태에서 실제 모델 경로를 지정합니다. 다음 경로는 예시입니다.

```bash
ros2 run fall_patrol_navigation navigation --ros-args \
  -p model_path:=/absolute/path/to/yolov8n.pt
```

`model_path`는 필수이며 `~`를 해석합니다. 현재 코드는 클래스 번호 `0`을 선택하고 이름이 `person`인 검출을 정지 대상으로 사용하므로, 이에 맞는 모델을 지정합니다. 파일이 없거나 모델 로딩이 실패하면 주행을 차단하며 자동 다운로드하지 않습니다.
입력은 `/map`, `/amcl_pose`, `/scan`, `/goal_pose`, `/image_raw/compressed`, `/tf`, `/tf_static`이고, 출력은 `/cmd_vel`, `/astar_path`입니다. RViz의 `2D Pose Estimate`로 위치를 잡고 `2D Goal Pose`로 목표를 지정합니다. 지도 원점의 yaw는 0인 구성을 지원합니다.

이 노드와 다른 주행 노드·teleop·Nav2 controller가 `/cmd_vel`을 동시에 발행하지 않도록 합니다. 종료는 해당 터미널의 `Ctrl+C`를 사용합니다.

## 위치 유효성

주행 위치는 기존 `/amcl_pose`를 사용합니다. AMCL 메시지의 수신 간격에는 별도 타임아웃을 걸지 않고, `map → base_footprint` TF가 존재하며 변환 시각이 유효한지 추가 확인합니다. 조회 실패·오래된 TF·잘못된 위치 값은 정지를 유지하며, 조건이 복구되면 기존 센서 정지 조건을 다시 확인합니다.

| 파라미터 | 기본값 | 용도 |
|---|---|---|
| `model_path` | 빈 문자열 | 직접 준비한 사람 감지 모델 경로 |
| `base_frame` | `base_footprint` | 위치 확인 대상 프레임 |
| `localization_max_age_sec` | `1.0` | TF 시각과 유효성 확인 결과의 최대 경과 시간 |
| `localization_future_tolerance_sec` | `0.1` | 허용할 TF의 미래 시각 오차 |
| `yolo_rate_hz` | `8.0` | 영상 디코딩·추론 빈도 상한 |
| `yolo_cpu_threads` | `2` | PyTorch CPU 스레드 수 |

시간·속도·가감속 기본값은 실기 검증이 끝난 운영값이 아닙니다. 실제 TF 주기·시계 동기화·센서 배치·정지 거리를 확인해야 합니다. TF 조회 성공만으로 위치 추정의 정확도까지 보장하지는 않습니다.

## 장비 없는 모의 테스트

프로젝트 루트에서 Python 3.10 이상의 표준 라이브러리만으로 실행합니다. ROS2·NumPy·모델을 모의 객체로 대체하며 로봇을 사용하지 않습니다.

```bash
python3 -B -m unittest discover -s src/fall_patrol_navigation/test -p 'test_*.py' -v
```

모델 경로 처리, TF 유효성·복구, 기존 정지 조건, 종료 순서와 일부 경로 계산을 검사합니다. 실제 ROS2 통신·모델 추론·주행 성능은 별도 검증 대상입니다.
