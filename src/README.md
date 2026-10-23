# fall_patrol_bridge — ROS와 서버를 잇는 패키지

로봇(ROS 2 Humble)에서 낙상 의심 감지 서버로 데이터를 보내는 ROS 패키지다.

- `status_bridge` 노드: 로봇 위치·방향·상태를 서버로 계속 보낸다 → 대시보드 지도에서 로봇이 움직인다
- `ServerClient`: 주행·인식 노드가 import해서 서버를 부르는 파이썬 클래스 (사람 감지 → 이벤트 기록)

서버는 같은 PC에서 `http://127.0.0.1:8000`으로 떠 있다고 가정한다. 서버 실행은 [../README.md](../README.md), 주행 노드에서 쓰는 방법 전체는 [../docs/API.md](../docs/API.md) 참고.

## 빌드

```bash
mkdir -p ~/fall_patrol_ws/src
cp -r ros/fall_patrol_bridge ~/fall_patrol_ws/src/
cd ~/fall_patrol_ws
colcon build --packages-select fall_patrol_bridge
source install/setup.bash
```

## status_bridge: 로봇 위치를 서버로 보내기

```bash
ros2 run fall_patrol_bridge status_bridge
```

- TF `map → base_footprint`를 0.2초마다 읽어서 `PUT /status`로 보낸다. 위치 추정(AMCL)이 떠 있어야 한다
- 시뮬레이션에서는 `--ros-args -p use_sim_time:=true`

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `server_url` | `http://127.0.0.1:8000` | 서버 주소 |
| `rate_hz` | `5.0` | 전송 주기 |
| `map_frame` | `map` | 지도 좌표계 |
| `base_frame` | `base_footprint` | 로봇 좌표계 |
| `initial_state` | `patrol` | 상태 토픽을 받기 전까지 쓸 상태 |

### 주행 노드가 발행하면 되는 토픽

| 토픽 | 타입 | 값 |
|---|---|---|
| `/fall_patrol/state` | `std_msgs/String` | `idle`, `patrol`, `suspect`, `approach`, `observe`, `alert` 중 하나. 다른 값은 무시 |
| `/fall_patrol/waypoint` | `std_msgs/Int32` | 지금 가고 있는 순찰 지점 번호 (선택) |

상태가 바뀔 때만 한 번씩 발행하면 된다. 대시보드의 로봇 상태창과 실시간 로그에 바로 나온다.

## ServerClient: 사람을 감지했을 때 서버에 기록하기

```python
import cv2
from fall_patrol_bridge.server_client import ServerClient

client = ServerClient()  # 기본 http://127.0.0.1:8000

def jpg(frame):
    return cv2.imencode(".jpg", frame)[1].tobytes(), "image/jpeg"

# 1. 누운 사람 발견: 사진 ①을 들고 있다가 무시 구역인지 확인
detected = jpg(frame_when_detected)
if client.is_ignored(x, y):
    ...  # 이미 처리 중인 위치 → 사진 버리고 순찰 계속

# 2. 접근해서 멈춤: 사진 ②와 함께 이벤트 생성
event_id = client.create_event(x, y, confidence, detected, jpg(frame_when_arrived))

# 3. 관찰 끝: 사진 ③과 결과
client.add_snapshot(event_id, "decision", jpg(best_frame))
client.set_result(event_id, "confirmed")  # confirmed | uncertain | false_alarm
```

- `is_ignored`는 서버가 응답하지 않으면 `False`를 돌려준다. 놓치는 것보다 한 번 더 확인하는 쪽이 안전하기 때문이다
- 나머지 함수는 서버 오류 시 `ServerError`를 던진다. 순찰이 멈추지 않게 `try/except ServerError`로 감싸서 쓴다
- 좌표 `x, y`는 map 좌표계(미터)다
- 표준 라이브러리만 써서 추가 설치가 필요 없다

## 시뮬레이션에서 확인한 것 (2026-10-22, Gazebo turtlebot3_world, waffle_pi)

- 로봇을 움직이면 서버의 위치·방향이 따라 바뀌고 대시보드 지도에 맞는 위치로 표시됨
- `/fall_patrol/state`, `/fall_patrol/waypoint`가 서버에 반영되고, 잘못된 상태 값은 무시됨
- `ServerClient`로 이벤트 생성 → 사진 ③ → 결과 전송 → 같은 위치 무시 판정까지 동작
- 서버가 꺼져 있을 때 `is_ignored`는 `False`, `create_event`는 `ServerError`
