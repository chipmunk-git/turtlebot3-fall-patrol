import argparse
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from scripts.patrol import Patrol, Point, load_patrol
from scripts.png_utils import decode_gray_png, make_snapshot

TICK = 0.2
RECHECK_COOLDOWN = 15.0
# 실제 로봇(src/my_bot_fall/my_bot_fall/patrol_logic.py)과 같은 세 가지 결말.
# 오탐(false_alarm)은 로봇이 정하지 않고, 사람이 웹에서 사진을 보고 누른다.
SCENARIOS = ["confirmed", "normal", "uncertain"]
OBSERVATION_TIMEOUT = 15.0  # 이 시간 안에 낙상을 확정하지 못하면 확인 필요
NORMAL_CLEAR_SECONDS = 2.0  # 정상 자세가 이만큼 이어지면 이벤트 없이 순찰 재개
WEB_DONE = ("acknowledged", "resolved", "false_alarm")
LABELS = {"confirmed": "낙상 의심", "uncertain": "확인 필요"}


class MapView:
    def __init__(self, info: dict[str, Any], image: bytes) -> None:
        self.resolution = info["resolution"]
        self.origin_x = info["origin_x"]
        self.origin_y = info["origin_y"]
        self.cos = math.cos(info.get("origin_yaw", 0.0))
        self.sin = math.sin(info.get("origin_yaw", 0.0))
        self.width, self.height, self.pixels = decode_gray_png(image)

    def is_free(self, x: float, y: float) -> bool:
        dx, dy = x - self.origin_x, y - self.origin_y
        col = int((dx * self.cos + dy * self.sin) / self.resolution)
        row = self.height - 1 - int((dy * self.cos - dx * self.sin) / self.resolution)
        return 0 <= col < self.width and 0 <= row < self.height and self.pixels[row * self.width + col] > 250

    def line_of_sight(self, start: Point, end: Point) -> bool:
        steps = max(1, int(math.dist(start, end) / (self.resolution / 2)))
        return all(
            self.is_free(start[0] + (end[0] - start[0]) * k / steps, start[1] + (end[1] - start[1]) * k / steps)
            for k in range(steps + 1)
        )


class FakeRobot:
    def __init__(
        self,
        base_url: str,
        patrol: Patrol,
        speed: float,
        observe_seconds: float,
        rng: random.Random,
        random_results: bool = False,
        ack_wait: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.patrol = patrol
        self.speed = speed
        self.observe_seconds = observe_seconds
        self.rng = rng
        self.random_results = random_results
        self.ack_wait = ack_wait
        self.scenario_index = 0
        self.x, self.y = patrol.waypoints[0]
        self.yaw = 0.0
        self.waypoint = 0
        self.last_checked: dict[Point, float] = {}
        self.map: MapView | None = None

    def load_map(self) -> None:
        info = self.request("GET", "/map")
        if info is None:
            print("지도 없음: 벽을 무시하고 거리만으로 감지")
            return
        with urllib.request.urlopen(f"{self.base_url}{info['image_url']}", timeout=5) as response:
            self.map = MapView(info, response.read())
        print(f"지도 로드: {self.map.width}x{self.map.height}, 벽 너머의 사람은 감지하지 않음")

    def request(
        self, method: str, path: str, body: bytes | None = None, content_type: str | None = None
    ) -> Any:
        req = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"  ! {method} {path} 실패: {error}")
            return None

    def send_json(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        return self.request(method, path, json.dumps(payload).encode(), "application/json")

    def put_status(self, state: str) -> None:
        self.send_json(
            "PUT",
            "/status",
            {
                "state": state,
                "x": round(self.x, 3),
                "y": round(self.y, 3),
                "yaw": round(self.yaw, 3),
                "current_waypoint": self.waypoint,
            },
        )

    def post_multipart(self, path: str, fields: dict[str, Any], files: dict[str, bytes]) -> Any:
        boundary = uuid.uuid4().hex
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields.items()
        )
        for name, image in files.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{name}.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode()
            body += image + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        return self.request("POST", path, body, f"multipart/form-data; boundary={boundary}")

    def hold(self, state: str, seconds: float) -> None:
        for _ in range(int(seconds / TICK)):
            self.put_status(state)
            time.sleep(TICK)

    def wait_for_run(self) -> None:
        """명시적인 run 명령을 읽을 때까지 이동하지 않고 재조회한다."""
        waiting_for = None
        while True:
            try:
                control = self.request("GET", "/control")
            except (OSError, ValueError):
                control = None
            drive = control.get("drive") if isinstance(control, dict) else None
            if drive == "run":
                if waiting_for is not None:
                    print("[주행] 웹에서 주행 시작: 이어서 이동")
                return
            reason = "stop" if drive == "stop" else "unavailable"
            if waiting_for != reason:
                print("[주행] 웹에서 정지: 시작 명령을 기다림" if reason == "stop" else
                      "[주행] 주행 명령 조회 실패 또는 응답 오류: 이동하지 않고 재조회")
                waiting_for = reason
            try:
                self.put_status("idle")
            except (OSError, ValueError):
                pass  # 상태 보고 실패가 이동을 허용하지는 않는다.
            time.sleep(0.5)

    def drive_to(self, target: Point, state: str, stop_distance: float = 0.0) -> None:
        while True:
            self.wait_for_run()
            dx, dy = target[0] - self.x, target[1] - self.y
            distance = math.hypot(dx, dy)
            if distance <= stop_distance + 1e-6:
                return
            self.yaw = math.atan2(dy, dx)
            step = min(self.speed * TICK, distance - stop_distance)
            self.x += step * math.cos(self.yaw)
            self.y += step * math.sin(self.yaw)
            self.put_status(state)
            time.sleep(TICK)
            if state == "patrol":
                self.look_around()

    def look_around(self) -> None:
        now = time.monotonic()
        for spot in self.patrol.person_spots:
            if math.hypot(spot[0] - self.x, spot[1] - self.y) > self.patrol.detect_range:
                continue
            if self.map and not self.map.line_of_sight((self.x, self.y), spot):
                continue
            if now - self.last_checked.get(spot, -math.inf) < RECHECK_COOLDOWN:
                continue
            self.last_checked[spot] = now
            self.handle_person(spot)

    def next_scenario(self) -> str:
        if self.random_results:
            return self.rng.choice(SCENARIOS)
        scenario = SCENARIOS[self.scenario_index % len(SCENARIOS)]
        self.scenario_index += 1
        return scenario

    def handle_person(self, spot: Point) -> None:
        x = round(spot[0] + self.rng.uniform(-0.15, 0.15), 2)
        y = round(spot[1] + self.rng.uniform(-0.15, 0.15), 2)
        seed = self.rng.randrange(1 << 30)
        detected_snapshot = make_snapshot(seed, "detected")
        print(f"[감지] 누운 자세 의심 ({x}, {y}), 사진 ① 보관")
        self.hold("suspect", 1.0)

        query = urllib.parse.urlencode({"x": x, "y": y})
        check = self.request("GET", f"/ignore-zones/check?{query}")
        if check and check["ignored"]:
            print(f"  → 무시 구역 (이벤트 {check['event_id']}), 사진 ① 버리고 순찰 계속")
            return

        print("  → 접근 후 정지 관찰")
        self.drive_to(spot, "approach", self.patrol.approach_distance)
        scenario = self.next_scenario()
        if scenario == "normal":
            self.hold("observe", NORMAL_CLEAR_SECONDS + 1.0)
            print(f"  → 정상 자세가 {NORMAL_CLEAR_SECONDS:.0f}초 이어짐: 이벤트 없이 순찰 재개")
            return
        if scenario == "confirmed":
            self.hold("observe", self.observe_seconds)
            print(f"  → 누운 자세·저움직임 {self.observe_seconds:.0f}초 연속 확인")
            confidence = self.rng.uniform(0.75, 0.95)
        else:
            self.hold("observe", OBSERVATION_TIMEOUT)
            print(f"  → {OBSERVATION_TIMEOUT:.0f}초 동안 낙상 여부를 확정하지 못함")
            confidence = self.rng.uniform(0.4, 0.65)

        event = self.post_multipart(
            "/events",
            {"x": x, "y": y, "confidence": round(confidence, 2), "status": scenario},
            {"detected_snapshot": detected_snapshot, "observing_snapshot": make_snapshot(seed, "observing")},
        )
        if event is None:
            return
        self.post_multipart(
            f"/events/{event['id']}/snapshots",
            {"kind": "decision"},
            {"snapshot": make_snapshot(seed, "decision")},
        )
        print(f"  → 이벤트 {event['id']} 생성: {LABELS[scenario]} (사진 ①②③), 웹 확인 대기")
        self.wait_for_web(event["id"])

    def wait_for_web(self, event_id: int) -> None:
        """실제 로봇처럼 웹에서 확인·처리완료·오탐을 누를 때까지 알림 상태로 멈춘다."""
        deadline = time.monotonic() + self.ack_wait
        while self.ack_wait <= 0 or time.monotonic() < deadline:
            event = self.request("GET", f"/events/{event_id}")
            if event and event["status"] in WEB_DONE:
                print(f"  → 웹에서 {event['status']} 처리: 순찰 재개")
                return
            self.hold("alert", 1.0)
        print(f"  → {self.ack_wait:.0f}초 동안 웹 처리 없음: 시연용으로 순찰 재개")

    def run(self) -> None:
        print(f"fake robot → {self.base_url} (Ctrl+C로 종료)")
        self.load_map()
        while True:
            self.waypoint = (self.waypoint + 1) % len(self.patrol.waypoints)
            print(f"[순찰] {self.waypoint}번 지점으로 이동")
            self.drive_to(self.patrol.waypoints[self.waypoint], "patrol")


def main() -> None:
    parser = argparse.ArgumentParser(description="로봇 대신 서버 API를 호출하는 가짜 로봇")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--speed", type=float, default=0.3, help="이동 속도 (m/s)")
    parser.add_argument("--observe", type=float, default=5.0, help="낙상 의심으로 확정하기까지 관찰 시간 (초)")
    parser.add_argument("--random", action="store_true", help="결말을 차례대로가 아니라 무작위로 고름")
    parser.add_argument("--ack-wait", type=float, default=30.0, help="알림 뒤 웹 처리를 기다리는 시간 (초, 0이면 무한)")
    args = parser.parse_args()

    robot = FakeRobot(
        args.url, load_patrol(), args.speed, args.observe, random.Random(), args.random, args.ack_wait
    )
    try:
        robot.run()
    except KeyboardInterrupt:
        robot.put_status("idle")
        print("종료")


if __name__ == "__main__":
    main()
