# 주행 연동 가이드

낙상 의심 감지 대시보드에 로봇 위치와 사람 감지 결과가 나오게 하려면 주행 노드에서 아래 두 가지만 하면 된다. 서버·대시보드·ROS 노드가 모두 **같은 리눅스 PC**에서 돈다는 전제다.

| 할 일 | 방법 | 대시보드에 나오는 것 |
|---|---|---|
| 1. 로봇 위치·상태 보내기 | `status_bridge` 노드 실행 + 상태 토픽 발행 | 지도 위 로봇 위치·시야각, 로봇 상태창 |
| 2. 사람 감지 결과 보내기 | `ServerClient` 함수 호출 | 알림 배너, 이벤트 기록, 사진 3장 |

둘 다 `ros/fall_patrol_bridge` 패키지에 들어 있다.

## 0. 준비 (처음 한 번)

```bash
mkdir -p ~/fall_patrol_ws/src
cp -r ~/project_3/ros/fall_patrol_bridge ~/fall_patrol_ws/src/
cd ~/fall_patrol_ws && colcon build --packages-select fall_patrol_bridge
source ~/fall_patrol_ws/install/setup.bash
```

서버는 [../README.md](../README.md)대로 켜 둔다. 서버가 꺼져 있어도 주행 노드가 멈추지는 않는다.

## 1. 로봇 위치·상태

### 위치: status_bridge 실행

```bash
ros2 run fall_patrol_bridge status_bridge
```

- TF `map → base_footprint`에서 로봇 위치와 방향을 읽어 0.2초마다 서버로 보낸다
- **위치 추정(AMCL)이 켜져 있어야 한다.** 꺼져 있으면 "map 좌표의 로봇 위치를 아직 모름" 경고가 나온다
- AMCL이 쓰는 지도와 대시보드 지도가 **같은 지도**여야 로봇이 제자리에 표시된다

### 상태: 바뀔 때 토픽 발행

| 토픽 | 타입 | 값 |
|---|---|---|
| `/fall_patrol/state` | `std_msgs/String` | 아래 상태 중 하나 |
| `/fall_patrol/waypoint` | `std_msgs/Int32` | 지금 가는 순찰 지점 번호 (선택) |

| 값 | 대시보드 표시 | 언제 발행 |
|---|---|---|
| `idle` | 대기 | 순찰 시작 전, 정지 |
| `patrol` | 순찰 중 | 순찰 경로로 이동할 때 |
| `suspect` | 의심 감지 | 누운 사람을 처음 발견한 순간 |
| `approach` | 접근 중 | 사람에게 다가가기 시작할 때 |
| `observe` | 관찰 중 | 멈춰서 움직임을 지켜보기 시작할 때 |
| `alert` | 알림 | 낙상 의심으로 판단한 직후 (잠깐) |

다른 값을 보내면 무시된다. 발행하지 않으면 계속 "순찰 중"으로 보인다.

```python
from std_msgs.msg import String
self.state_pub = self.create_publisher(String, "/fall_patrol/state", 10)

def set_state(self, state):
    self.state_pub.publish(String(data=state))
```

## 2. 사람 감지 결과

### 흐름

사진은 3장이다: ① 처음 발견한 순간(멀리서), ② 접근해서 멈춘 순간, ③ 판단을 내린 순간.

```
1. 누운 사람 발견      상태 suspect, 사진 ① 찍어서 들고 있기
2. 무시 구역인가?      client.is_ignored(x, y)
     True  →          사진 ① 버리고 상태 patrol, 순찰 계속
     False →          상태 approach, 접근
3. 도착해서 멈춤       상태 observe, 사진 ②
                      event_id = client.create_event(...)    ← 이 순간부터 이 위치는 무시 구역
4. 관찰 (15~30초)
5. 판단               사진 ③
                      client.add_snapshot(event_id, "decision", ...)
                      client.set_result(event_id, 결과)        결과가 confirmed면 상태 alert
6. 순찰 재개           상태 patrol
```

### ServerClient 함수

```python
from fall_patrol_bridge.server_client import ServerClient, ServerError
client = ServerClient()
```

| 함수 | 언제 | 인자 | 돌려주는 값 |
|---|---|---|---|
| `is_ignored(x, y)` | 2. 다가가기 전 | 사건 위치 | `True`면 이미 처리 중인 위치 → 다가가지 않음 |
| `create_event(x, y, confidence, detected_image, observing_image)` | 3. 멈췄을 때 | 위치, 인식 신뢰도(0~1), 사진 ①②| 이벤트 번호 `event_id` (저장해 둘 것) |
| `add_snapshot(event_id, "decision", image)` | 5. 판단할 때 | 이벤트 번호, 사진 ③ | 없음 |
| `set_result(event_id, status)` | 5. 판단할 때 | 이벤트 번호, 결과 | 없음 |

- 좌표 `x, y`는 **map 좌표계, 미터**. **로봇 위치가 아니라 사람이 있는 위치**를 넣는다 (아래 "사람 좌표" 참고)
- 사진은 `(바이트, "image/jpeg")` 형태 (아래 `capture()` 참고)
- `is_ignored`는 서버가 응답하지 않으면 `False`를 돌려준다. 놓치는 것보다 한 번 더 보는 게 안전하다
- 나머지 함수는 서버 오류 시 `ServerError`를 던진다. 순찰이 멈추지 않게 `try/except`로 감싼다

### 판단 결과 값

| 값 | 뜻 | 대시보드 |
|---|---|---|
| `confirmed` | 낙상 의심 (관찰 시간 동안 움직임 없음) | 빨간 알림 배너 |
| `uncertain` | 판단 불가 (가려짐, 일부만 보임, 애매함) | 노란 알림 배너, 관리자가 판단 |
| `false_alarm` | 오탐 (일어남, 걸어감, 사람이 아님) | 알림 없음 |

"확인", "처리 완료"는 관리자가 대시보드에서 누른다. 주행 노드는 위 세 가지만 보낸다.

### 무시 구역 기준 (서버가 계산)

이미 기록된 이벤트에서 **1m 이내**이고 아래에 해당하면 `is_ignored`가 `True`다.

- 낙상 의심·판단 불가·관찰 중·확인됨 상태이고 **10분 이내**
- 오탐이고 **3분 이내**
- 관리자가 "처리 완료"를 누른 이벤트는 제외 (같은 자리에서 다시 감지해야 하므로)

### 사람 좌표

서버는 받은 좌표를 그대로 저장하고, 대시보드 지도에 그 위치로 점을 찍고, 다음 `is_ignored` 때 그 좌표에서 1m 안인지로 무시 구역을 계산한다. 그래서 **`is_ignored`와 `create_event`에 둘 다 "사람이 있는 위치"를 넣어야** 무시 구역이 제대로 동작한다.

로봇 위치를 그대로 넣으면 같은 사람을 여러 번 기록하게 된다.

```
1바퀴: 2.5m 떨어진 곳에서 발견 → 다가가서 사람 앞 1.2m에 멈춤 → 이벤트 좌표 = 멈춘 로봇 위치 A
2바퀴: 다른 방향, 2m 떨어진 곳에서 발견 → is_ignored(지금 로봇 위치 B)
       A와 B가 1m 넘게 떨어져 있음 → "무시 구역 아님" → 같은 사람에게 또 접근, 이벤트 또 생성
```

사람 위치는 어느 방향에서 봐도 같은 자리라서 이런 문제가 없다.

| 방법 | 계산 | 정확도 |
|---|---|---|
| 간단 | 로봇 위치에서 바라보는 방향으로 사람까지의 거리만큼 앞 | 대략 맞음 |
| 정확 | 라이다나 뎁스로 사람까지 거리·방향을 재서 그 지점 | 정확 |

```python
def person_position(robot_x, robot_y, robot_yaw, distance):
    return robot_x + distance * math.cos(robot_yaw), robot_y + distance * math.sin(robot_yaw)

x, y = person_position(rx, ry, ryaw, 2.5)   # 발견했을 때: 대략 추정한 거리 (예: 2.5m)
...
x, y = person_position(rx, ry, ryaw, 1.2)   # 멈춘 뒤: 접근 거리 (예: 1.2m)
```

- 로봇 위치·방향은 TF `map → base_footprint`에서 얻는다 (`status_bridge`가 쓰는 것과 같은 값)
- 사람이 화면 정중앙이 아니면 방향이 조금 틀어지지만, 무시 구역 반경이 1m라 대략 맞으면 충분하다
- 멈춘 뒤 계산한 좌표가 더 정확하므로 `create_event`에는 멈춘 뒤 좌표를 쓴다

### 사진 찍기

"사진을 찍는다"는 건 카메라 토픽에서 **그 순간의 최신 화면 한 장을 꺼내는 것**이다.

```python
from sensor_msgs.msg import CompressedImage
import cv2, numpy as np

self.latest = None
self.create_subscription(CompressedImage, "/image_raw/compressed", self.on_image, 10)

def on_image(self, msg):
    self.latest = msg                       # 계속 최신 화면으로 덮어씀

def capture(self):
    frame = cv2.imdecode(np.frombuffer(self.latest.data, np.uint8), cv2.IMREAD_COLOR)
    return cv2.imencode(".jpg", frame)[1].tobytes(), "image/jpeg"
```

인식 노드가 박스·스켈레톤을 그린 화면을 토픽으로 발행하면, 구독 토픽만 그걸로 바꾸면 된다. 관리자가 판단하기 훨씬 쉽다.

### 전체 예시

```python
detected = self.capture()                                 # 1. 발견 (사진 ①)
self.set_state("suspect")
if self.client.is_ignored(x, y):                          # 2. 무시 구역
    self.set_state("patrol")
    return
self.set_state("approach")
...                                                       # 접근해서 멈춤
self.set_state("observe")
try:
    event_id = self.client.create_event(x, y, confidence, detected, self.capture())   # 3.
    ...                                                   # 4. 관찰
    self.client.add_snapshot(event_id, "decision", self.capture())                    # 5.
    self.client.set_result(event_id, result)
    if result == "confirmed":
        self.set_state("alert")
except ServerError as error:
    self.get_logger().error(f"서버 기록 실패: {error}")    # 순찰은 계속
self.set_state("patrol")                                  # 6. 순찰 재개
```

## 3. 같이 정해야 할 것

| 항목 | 선택지 | 제안 |
|---|---|---|
| 사람 좌표 `x, y` | 로봇 앞 거리로 추정 / 라이다·뎁스로 측정 | **로봇 위치 그대로는 안 됨**. 처음엔 "바라보는 방향으로 거리만큼 앞"으로 추정 (위 "사람 좌표" 참고) |
| 관찰 시간 | 몇 초 동안 안 움직이면 낙상 의심인지 | 15~30초 |
| 접근 거리 | 사람 앞 몇 m에서 멈출지 | 1~1.5m (카메라에 몸 전체가 들어오는 거리) |
| 지도 | 새로 매핑하면 `.yaml` + `.pgm` 공유 | 대시보드 지도도 같은 지도로 교체 |
| 사진 화면 | 원본 카메라 / 인식이 그린 화면 | 인식이 그린 화면 |

## 4. 잘 되는지 확인

브라우저에서 `http://localhost:8000` 대시보드를 열어 둔다.

- `status_bridge`를 켜면 → 지도에 로봇이 나타나고, 움직이면 따라온다
- 상태 토픽을 발행하면 → 로봇 상태창과 실시간 로그가 바뀐다
- 사람 감지 흐름을 돌리면 → 알림 배너가 뜨고, 이벤트를 누르면 사진 3장과 처리 이력이 보인다

`ServerClient`는 내부적으로 같은 PC의 서버(`127.0.0.1:8000`)와 통신한다. 주행 노드에서 신경 쓸 필요는 없다.
