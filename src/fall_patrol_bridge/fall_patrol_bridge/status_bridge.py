import math

import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.time import Time
from std_msgs.msg import Int32, String
from tf2_ros import Buffer, TransformException, TransformListener

from fall_patrol_bridge.server_client import ROBOT_STATES, ServerClient, ServerError


def yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class StatusBridge(Node):
    def __init__(self) -> None:
        super().__init__("status_bridge")
        server_url = self.declare_parameter("server_url", "http://127.0.0.1:8000").value
        rate_hz = self.declare_parameter("rate_hz", 5.0).value
        self.map_frame = self.declare_parameter("map_frame", "map").value
        self.base_frame = self.declare_parameter("base_frame", "base_footprint").value
        self.state = self.declare_parameter("initial_state", "patrol").value

        self.client = ServerClient(server_url, timeout=0.5)
        self.waypoint: int | None = None
        self.server_ok = True
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self.create_subscription(String, "/fall_patrol/state", self.on_state, 10)
        self.create_subscription(Int32, "/fall_patrol/waypoint", self.on_waypoint, 10)
        self.create_timer(1.0 / rate_hz, self.send_status)
        self.get_logger().info(f"{self.map_frame} -> {self.base_frame} 위치를 {server_url} 로 전송")

    def on_state(self, msg: String) -> None:
        if msg.data not in ROBOT_STATES:
            self.get_logger().warn(f"알 수 없는 상태 '{msg.data}', 가능한 값: {', '.join(ROBOT_STATES)}")
            return
        self.state = msg.data

    def on_waypoint(self, msg: Int32) -> None:
        self.waypoint = msg.data

    def current_pose(self) -> tuple[float, float, float] | None:
        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_frame, self.base_frame, Time(), timeout=Duration(seconds=0.05)
            )
        except TransformException:
            return None
        t, q = transform.transform.translation, transform.transform.rotation
        return t.x, t.y, yaw_from_quaternion(q.x, q.y, q.z, q.w)

    def send_status(self) -> None:
        pose = self.current_pose()
        if pose is None:
            self.get_logger().warn("map 좌표의 로봇 위치를 아직 모름 (위치 추정 대기)", throttle_duration_sec=5.0)
            return
        x, y, yaw = pose
        try:
            self.client.put_status(self.state, round(x, 3), round(y, 3), round(yaw, 3), self.waypoint)
        except ServerError as error:
            if self.server_ok:
                self.get_logger().error(f"서버 전송 실패: {error}")
            self.server_ok = False
            return
        if not self.server_ok:
            self.get_logger().info("서버 연결 복구")
        self.server_ok = True


def main() -> None:
    rclpy.init()
    node = StatusBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
