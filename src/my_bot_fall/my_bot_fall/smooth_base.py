"""부드러운 A* 주행 수정본 (첨부한 원본 기준, 사람 근접 순찰 테스트 확장).
직진 상한 0.15m/s; 가속 0.12m/s^2; 정상 감속 0.20m/s^2.
벽 근처 비용 / 20Hz 속도 제한 / 회피 방향 유지 / 종료 히스테리시스.
긴급정지와 센서 오류는 가감속 제한을 건너뛰고 즉시 0을 발행합니다.
scan은 base와 축이 정렬된 기존 TB3 장착을 전제로 합니다.
로봇 중심과 라이다 중심 차이, AMCL 오차, 실제 제동 성능은 현장 검증 필요.
너무 가까운 물체가 있거나 경로가 없으면 정지 유지합니다. 무리한 탈출 회전 없음.
하단 PersonPatrolTest가 반복 순찰/임시 관찰/웹 확인을 담당합니다. 낙상 판정 기능은 없습니다.
"""
import heapq
import math
import os
import threading
import time
from math import atan2, sin, cos, sqrt, pi

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from nav_msgs.msg import OccupancyGrid, Path
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
from sensor_msgs.msg import LaserScan, Image, CompressedImage

# 비상정지용 (터틀봇3 카메라 토픽 + CvBridge + YOLO).
# 비전 의존성/모델이 없으면 주행을 차단한다.
IMPORT_ERROR = ''
try:
    import cv2
    from cv_bridge import CvBridge
    from ultralytics import YOLO
except Exception as e:
    cv2 = None
    CvBridge = None
    YOLO = None
    IMPORT_ERROR = str(e)


# ---------------------------------------------------------------
# 1) 순수 계산 함수들 (ROS와 상관없는 부분: 따로 테스트하기 쉬움)
# ---------------------------------------------------------------

def world_to_grid(x, y, origin, resolution):
    """미터 좌표 (x, y) -> 칸 번호 (행, 열)"""
    col = math.floor((x - origin[0]) / resolution)
    row = math.floor((y - origin[1]) / resolution)
    return (row, col)


def grid_to_world(row, col, origin, resolution):
    """칸 번호 (행, 열) -> 칸의 한가운데 미터 좌표 (x, y)"""
    x = origin[0] + (col + 0.5) * resolution
    y = origin[1] + (row + 0.5) * resolution
    return (x, y)


def make_blocked_grid(map_data, inflate_cells):
    """0이 아닌 칸(벽 100, 미확인 -1)을 막힌 곳(True)으로 보고,
    로봇 몸집만큼 사방으로 부풀린다. (벽에 붙어 가다 긁히는 것을 방지)"""
    blocked = (map_data != 0)
    if inflate_cells <= 0:
        return blocked

    h, w = blocked.shape
    inflated = blocked.copy()
    r = inflate_cells
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx > r * r:
                continue
            # blocked 배열을 (dy, dx)만큼 밀어서 겹쳐 놓는다
            src_y0, src_y1 = max(0, -dy), min(h, h - dy)
            src_x0, src_x1 = max(0, -dx), min(w, w - dx)
            dst_y0, dst_y1 = max(0, dy), min(h, h + dy)
            dst_x0, dst_x1 = max(0, dx), min(w, w + dx)
            if src_y1 <= src_y0 or src_x1 <= src_x0:
                continue
            inflated[dst_y0:dst_y1, dst_x0:dst_x1] |= blocked[src_y0:src_y1, src_x0:src_x1]
    return inflated


def run_astar(blocked, start, end, extra_cost=None):
    """8방향 A*. blocked[행][열]이 True면 못 가는 칸.
    - 직선 이동 비용 1, 대각선 비용 sqrt(2)
    - 대각선 이동 시 옆의 두 칸 중 하나라도 막혀 있으면 모서리를 긁으므로 금지
    - h(남은 거리 짐작)는 8방향에 맞는 octile 거리
    """
    height, width = blocked.shape
    sr, sc = start
    er, ec = end

    if not (0 <= sr < height and 0 <= sc < width): return None
    if not (0 <= er < height and 0 <= ec < width): return None
    if blocked[er][ec] or blocked[sr][sc]: return None

    SQ2 = sqrt(2.0)
    moves = [(0, 1, 1.0), (0, -1, 1.0), (1, 0, 1.0), (-1, 0, 1.0),
             (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2)]

    def heuristic(r, c):
        dr, dc = abs(r - er), abs(c - ec)
        return (dr + dc) + (SQ2 - 2.0) * min(dr, dc)

    g_best = {start: 0.0}       # 각 칸까지 지금까지 찾은 가장 싼 비용
    parent = {start: None}      # 어느 칸에서 왔는지 (경로 복원용)
    counter = 0                 # f가 같을 때 비교 오류를 막는 번호표
    open_list = [(heuristic(sr, sc), counter, start)]
    visited = set()

    while open_list:
        f, _, cur = heapq.heappop(open_list)
        if cur in visited:
            continue
        visited.add(cur)

        if cur == end:
            path = []
            while cur is not None:
                path.append(cur)
                cur = parent[cur]
            return path[::-1]

        cr, cc = cur
        for dr, dc, cost in moves:
            nr, nc = cr + dr, cc + dc
            if not (0 <= nr < height and 0 <= nc < width):
                continue
            if blocked[nr][nc]:
                continue
            if (nr, nc) in visited:
                continue
            if dr != 0 and dc != 0:
                if blocked[cr + dr][cc] or blocked[cr][cc + dc]:
                    continue

            penalty = 0.0 if extra_cost is None else float(extra_cost[nr, nc])
            new_g = g_best[cur] + cost * (1.0 + penalty)
            if new_g < g_best.get((nr, nc), float('inf')):
                g_best[(nr, nc)] = new_g
                parent[(nr, nc)] = cur
                counter += 1
                heapq.heappush(open_list, (new_g + heuristic(nr, nc), counter, (nr, nc)))

    return None


def clearance_cost(blocked, resolution, margin=0.30, weight=3.0):
    """Positive cost near inflated obstacles; hard footprint stays unchanged."""
    steps = max(1, int(math.ceil(margin / resolution)))
    penalty = np.zeros(blocked.shape, dtype=float)
    # Square rings conservatively penalize diagonal proximity too.
    reached = blocked.copy()
    for distance in range(1, steps + 1):
        padded = np.pad(reached, 1, constant_values=True)
        grown = np.zeros_like(reached)
        for dy in range(3):
            for dx in range(3):
                grown |= padded[dy:dy + reached.shape[0], dx:dx + reached.shape[1]]
        band = grown & ~reached
        penalty[band] = weight * ((steps - distance + 1) / steps) ** 2
        reached = grown
    return penalty


def slew(current, target, accel, decel, dt):
    """Rate limit; reverse direction through zero before accelerating again."""
    if current * target < 0:
        target = 0.0
    rate = accel if abs(target) > abs(current) else decel
    change = max(0.0, rate * dt)
    return current + max(-change, min(change, target - current))


def scan_sector(scan, center, half_width):
    """Angles in radians, independent of scan sample count/start angle.
    +inf means no return; NaN/zero/out-of-range samples are not evidence of clearance.
    """
    distances = []
    for i, r in enumerate(scan.ranges):
        a = scan.angle_min + i * scan.angle_increment
        delta = atan2(sin(a - center), cos(a - center))
        if abs(delta) > half_width:
            continue
        if math.isinf(r) and r > 0:
            distances.append(float(scan.range_max))
        elif math.isfinite(r) and max(0.05, scan.range_min) <= r <= scan.range_max:
            distances.append(r)
    return min(distances) if distances else 0.0


def segment_clear(blocked, start, end, origin, resolution):
    """Conservative supercover check to avoid lookahead shortcuts across corners."""
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    count = max(1, int(math.ceil(distance / (resolution * 0.25))))
    previous = None
    h, w = blocked.shape
    for i in range(count + 1):
        t = i / count
        cell = world_to_grid(start[0] + (end[0] - start[0]) * t,
                             start[1] + (end[1] - start[1]) * t, origin, resolution)
        r, c = cell
        if not (0 <= r < h and 0 <= c < w) or blocked[r, c]:
            return False
        if previous is not None and previous[0] != r and previous[1] != c:
            if blocked[previous[0], c] or blocked[r, previous[1]]:
                return False
        previous = cell
    return True


def find_nearest_free(blocked, cell, max_radius=20):
    """시작 위치가 부풀린 벽 안쪽에 걸렸을 때, 가장 가까운 빈 칸을 찾는다."""
    height, width = blocked.shape
    r0, c0 = cell
    if 0 <= r0 < height and 0 <= c0 < width and not blocked[r0][c0]:
        return cell
    for radius in range(1, max_radius + 1):
        best = None
        best_d = None
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < height and 0 <= c < width and not blocked[r][c]:
                    d = dr * dr + dc * dc
                    if best is None or d < best_d:
                        best, best_d = (r, c), d
        if best is not None:
            return best
    return None


def get_min_dist(range_list):
    """각도 구간 안에서 가장 가깝게 감지되는 거리 (유효하지 않은 값은 무시)"""
    valid_values = [x for x in range_list if x > 0.05 and x < 10.0]
    if valid_values:
        return min(valid_values)
    return 99.9


def mark_scan_obstacles(blocked, ranges, angle_min, angle_inc, pose, yaw,
                        origin, resolution, inflate_cells, max_range=1.0, half_fov=0.7):
    """라이다로 본 앞쪽 물체를 지도(blocked)에 임시 장애물로 표시한다.
    지도에 원래 없던 물체를 A*가 피해 가도록 만드는 부분.
    반환: 새로 막힌 칸 수"""
    height, width = blocked.shape
    newly = 0
    for i, r in enumerate(ranges):
        if not (0.05 < r < max_range):
            continue
        theta = angle_min + i * angle_inc
        while theta > pi: theta -= 2 * pi
        while theta < -pi: theta += 2 * pi
        if abs(theta) > half_fov:       # 앞쪽 약 ±40도만 사용
            continue
        wx = pose[0] + r * cos(yaw + theta)
        wy = pose[1] + r * sin(yaw + theta)
        row, col = world_to_grid(wx, wy, origin, resolution)
        for dr in range(-inflate_cells, inflate_cells + 1):
            for dc in range(-inflate_cells, inflate_cells + 1):
                if dr * dr + dc * dc > inflate_cells * inflate_cells:
                    continue
                rr, cc = row + dr, col + dc
                if 0 <= rr < height and 0 <= cc < width and not blocked[rr][cc]:
                    blocked[rr][cc] = True
                    newly += 1
    return newly


def pick_lookahead(path_xy, current_pose, lookahead, start_idx):
    """경로에서 로봇 앞쪽 lookahead(m) 만큼 떨어진 점을 고른다.
    start_idx 이후만 보기 때문에 경로를 되돌아가지 않는다.
    반환: (목표점, 로봇과 가장 가까운 경로 인덱스)"""
    # 1) 지금 로봇과 가장 가까운 경로 점 (이전 위치 근처에서만 찾는다)
    best_i = start_idx
    best_d = float('inf')
    for i in range(start_idx, min(start_idx + 60, len(path_xy))):
        d = math.hypot(path_xy[i][0] - current_pose[0], path_xy[i][1] - current_pose[1])
        if d < best_d:
            best_d, best_i = d, i

    # 2) 그 점부터 앞으로 가면서 lookahead 이상 떨어진 첫 점
    for i in range(best_i, len(path_xy)):
        d = math.hypot(path_xy[i][0] - current_pose[0], path_xy[i][1] - current_pose[1])
        if d >= lookahead:
            return path_xy[i], best_i
    return path_xy[-1], best_i


def calculate_velocity(current_pose, current_yaw, target_point, linear_vel):
    dx = target_point[0] - current_pose[0]
    dy = target_point[1] - current_pose[1]
    distance = sqrt(dx ** 2 + dy ** 2)
    if distance < 1e-3:
        return 0.0, 0.0

    alpha = atan2(dy, dx) - current_yaw
    while alpha > pi: alpha -= 2 * pi
    while alpha < -pi: alpha += 2 * pi

    angular_vel = linear_vel * (2.0 * sin(alpha)) / distance
    return angular_vel, alpha


def update_estop(active, detected, now, last_detect_time, release_delay):
    """비상정지 상태를 갱신한다. 반환: (정지 중인가, 마지막으로 물체를 본 시각)
    - 물체가 보이면 즉시 정지, 시각 갱신
    - 물체가 사라져도 release_delay초 동안은 정지 유지 (깜빡임으로 출발했다 멈췄다 하는 것 방지)"""
    if detected:
        return True, now
    if active and (now - last_detect_time) >= release_delay:
        return False, last_detect_time
    return active, last_detect_time


# ---------------------------------------------------------------
# 2) ROS2 노드
# ---------------------------------------------------------------

class AStarDriver(Node):
    def __init__(self):
        super().__init__('astar_driver_yolo')

        # --- 조절 가능한 값들 ---
        self.linear_speed = 0.15      # m/s (실제 터틀봇3에서 잘 동작한 값)
        self.max_angular = 0.45        # rad/s
        self.turn_in_place_angle = 0.8  # rad(약 45도) : 방향 오차가 이보다 크면 제자리 회전
        self.turn_speed = 0.30         # rad/s : 제자리 회전 속도
        self.lookahead = 0.35          # m : 앞을 얼마나 멀리 볼지
        self.goal_tolerance = 0.12    # m : 이 안에 들어오면 도착
        self.robot_radius = 0.25      # m : 벽 부풀리기 (waffle_pi 기준)
        self.obs_threshold = 0.30     # m : 이 거리 안에 앞 장애물이 있으면 회피 (벽 모서리에서 자꾸 멈추면 낮추기)
        self.stop_dist = 0.20         # m : 이보다 가까우면 새 경로가 있어도 비상 회피
        self.avoid_turn = 0.25         # rad/s : 회피할 때 제자리 회전 속도
        self.replan_cooldown = 1.5    # s : 장애물 때문에 재계획하는 최소 간격

        # Smooth motion tuning (m/s, m/s^2, rad/s^2).
        self.linear_accel = 0.12
        self.linear_decel = 0.20
        self.angular_accel = 0.70
        self.angular_decel = 1.00
        self.turn_exit_angle = 0.35
        self.goal_slow_distance = 0.50
        self.clearance_margin = 0.30
        self.clearance_weight = 3.0
        self.obstacle_release = 0.43
        self.release_hold = 0.4
        self.turning = False
        self.avoiding = False
        self.avoid_direction = 1.0
        self.clear_since = None
        self.last_cmd_v = 0.0
        self.last_cmd_w = 0.0
        self.last_cmd_time = time.monotonic()
        self.command_lock = threading.RLock()
        self.scan_time = 0.0
        self.scan_timeout = 0.6
        self.all_dist = 0.0
        self.cost_grid = None
        self.vision_fault = False
        self.inference_stale = True
        self.closing = False

        # --- 비상정지 (터틀봇3 카메라 + YOLO) ---
        self.estop_enabled = True            # False로 바꾸면 비상정지 기능을 끈다
        self.estop_classes = ['person']      # 이 이름의 객체만 정지 대상
        self.estop_conf = 0.7                # 이 확신도 이상일 때만 인정
        self.estop_release_delay = 0.8       # s : 사람이 사라진 뒤 이만큼 기다렸다가 주행 재개 (작을수록 빨리 출발, 너무 작으면 깜빡이며 멈췄다 가기를 반복)
        self.estop_period = 0.05             # s : 정지 판단/화면 갱신 주기 (가벼운 계산이라 빠르게 돌려도 됨)
        self.estop_distance_threshold = 0.5  # m : 이 거리보다 가까울 때만 비상정지 (라이다 회피 0.35m보다 커야 먼저 멈춤)
        self.yolo_imgsz = 320                # YOLO 입력 크기 (작을수록 빠름, 카메라가 320이라 충분)
        self.use_compressed = True           # True: 압축 영상(JPEG)을 받는다 (와이파이에서 끊김이 훨씬 적음)
        self.image_topic = '/image_raw/compressed' if self.use_compressed else '/image_raw'
        self.frame_timeout = 1.0             # s : 이 시간 넘게 영상이 안 오면 안전을 위해 정지
        self.model_path = self.declare_parameter('model_path', '').value       # 사전학습 모델 (person 포함)
        self.yolo_classes = [0]              # YOLO가 사람(번호 0)만 찾게 한다 -> 계산량도 줄어 덜 버벅임
        self.show_window = False              # 검출 화면 창 표시 (창이 필요 없으면 False)
        self.estop_active = False
        self.camera_stale = False            # 영상이 끊겼는가
        self.last_detect_time = 0.0
        self.last_frame_time = 0.0
        self.latest_frame = None
        self.camera_warned = False
        self.model = None
        self.detections = []                 # YOLO 스레드가 채워 두는 최신 검출 결과
        self.det_frame_time = 0.0            # 그 검출에 쓴 영상 시각
        self.running = True
        self.display_frame = None            # 화면에 보여줄 그림 (실제 창 띄우기는 메인 스레드가 한다)
        self.yolo_thread = None
        self.bridge = None
        self.frame_lock = threading.Lock()
        self.frame_event = threading.Event()
        self.pending_image = None
        self.result_snapshot = None
        self.latest_decoded_time = 0.0
        self.vision_last_error = ''
        self.last_vision_report = None
        self.yolo_rate_hz = float(self.declare_parameter('yolo_rate_hz', 8.0).value)
        self.yolo_cpu_threads = int(self.declare_parameter('yolo_cpu_threads', 2).value)
        if not 1.0 <= self.yolo_rate_hz <= 30.0 or self.yolo_cpu_threads < 1:
            raise ValueError('yolo_rate_hz: 1..30, yolo_cpu_threads >= 1')

        # --- 지도 정보 ---
        self.map_data = None
        self.base_blocked = None      # 원래 지도(벽을 부풀린 것)
        self.blocked = None           # base_blocked + 라이다로 발견한 임시 장애물

        # --- 라이다 ---
        self.last_scan = None
        self.front_dist = 99.9
        self.left_dist = 99.9
        self.right_dist = 99.9
        self.last_replan_time = 0.0
        self.map_resolution = 0.05
        self.map_origin = [0.0, 0.0]

        # --- 위치 / 목표 / 경로 ---
        self.current_pose = None
        self.current_yaw = 0.0
        self.goal_pose = None
        self.path_xy = []
        self.path_idx = 0
        self.need_plan = False

        # /map은 한 번만 발행되는 "게시판 공지"라서 Transient Local로 받아야 한다
        map_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE
        )
        self.create_subscription(OccupancyGrid, '/map', self.map_callback, map_qos)
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose', self.pose_callback, map_qos)
        self.create_subscription(PoseStamped, '/goal_pose', self.goal_callback, 10)
        # 실제 터틀봇3의 /scan은 BEST_EFFORT로 발행된다. 기본 설정(RELIABLE)으로 구독하면
        # QoS가 맞지 않아 라이다 데이터를 한 개도 못 받는다.
        scan_qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.create_subscription(LaserScan, '/scan', self.scan_callback, scan_qos)

        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        # RViz에서 Path 디스플레이로 계획된 경로를 눈으로 확인하기 위한 토픽
        self.path_pub = self.create_publisher(Path, '/astar_path', 10)

        # 카메라/화면/비상정지 판단 전용 작업 줄. 주행 계산(A*)이 오래 걸려도 이 줄은 따로 계속 돈다
        self.cam_group = MutuallyExclusiveCallbackGroup()
        self.create_timer(0.05, self.control_loop)  # 20 Hz command smoothing
        if self.estop_enabled:
            self.setup_estop()
        self.get_logger().info('astar_driver_yolo 시작: RViz에서 2D Goal Pose를 찍어주세요')

    # ---------- 비상정지 (YOLO) ----------
    def setup_estop(self):
        """YOLO 모델과 카메라 토픽 구독을 준비한다. 실패하면 주행을 차단한다."""
        if YOLO is None or cv2 is None or CvBridge is None:
            self.get_logger().error(
                f'cv2/cv_bridge/ultralytics를 불러오지 못해 주행을 차단합니다: {IMPORT_ERROR}')
            self.vision_fault = True
            return
        if not os.path.isfile(self.model_path):
            self.get_logger().error(f'모델 파일이 없습니다: {self.model_path} -> 주행을 차단합니다')
            self.vision_fault = True
            return
        try:
            import torch
            torch.set_num_threads(self.yolo_cpu_threads)
            cv2.setNumThreads(1)
            self.model = YOLO(self.model_path)
        except Exception as exc:
            self.vision_fault = True
            self.get_logger().error(f'YOLO 모델 로딩 실패, 주행 차단: {exc}')
            return
        self.get_logger().info(f'모델 클래스: {self.model.names}')
        self.bridge = CvBridge()
        # 카메라 영상은 BEST_EFFORT로 구독해야 RELIABLE/BEST_EFFORT 어느 쪽 발행이든 받을 수 있다
        cam_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        msg_type = CompressedImage if self.use_compressed else Image
        self.create_subscription(msg_type, self.image_topic, self.image_callback, cam_qos,
                                 callback_group=self.cam_group)
        # YOLO는 느리므로 영상이 올 때마다 돌리지 않고, 최신 한 장만 일정 주기로 검사한다
        self.vision_guard_group = MutuallyExclusiveCallbackGroup()
        self.create_timer(self.estop_period, self.detect_callback, callback_group=self.vision_guard_group)
        # 무거운 YOLO 계산은 별도 스레드에서 돌려서 주행/영상 수신이 멈추지 않게 한다
        self.yolo_thread = threading.Thread(target=self.yolo_worker, daemon=True)
        self.yolo_thread.start()
        self.get_logger().info('영상 처리 수정 v1: 최신 메시지만 저장 / 디코딩·추론 분리 / CPU 스레드 제한')
        self.get_logger().info(
            f'비상정지 켜짐: {self.image_topic}에서 {self.estop_classes} 감지 시 정지 (conf>={self.estop_conf})')

    def image_callback(self, msg):
        # No decoding, inference or command lock in the receiver.
        received = time.monotonic()
        with self.frame_lock:
            self.pending_image = (msg, received)
            self.last_frame_time = received
        self.frame_event.set()

    def yolo_worker(self):
        """Decode only the latest message, then infer at a bounded rate."""
        next_start = 0.0
        while self.running:
            remaining = next_start-time.monotonic()
            if remaining > 0:
                time.sleep(min(0.02, remaining))
                continue
            if not self.frame_event.wait(timeout=0.1):
                continue
            with self.frame_lock:
                packet = self.pending_image
                self.pending_image = None
                self.frame_event.clear()
            if not self.running:
                break
            if packet is None:
                continue
            msg, received = packet
            if time.monotonic()-received >= self.frame_timeout:
                continue
            next_start = time.monotonic() + 1.0/self.yolo_rate_hz
            try:
                if self.use_compressed:
                    frame = self.bridge.compressed_imgmsg_to_cv2(msg, desired_encoding='bgr8')
                else:
                    frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
                if frame is None or frame.size == 0:
                    raise ValueError('empty decoded image')
                results = self.model(frame, imgsz=self.yolo_imgsz, classes=self.yolo_classes,
                                     conf=self.estop_conf, max_det=5, verbose=False)
                dets = []
                for result in results:
                    for box in result.boxes:
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        dets.append((x1, y1, x2, y2, float(box.conf[0]), self.model.names[int(box.cls[0])]))
                with self.frame_lock:
                    # Frame, detections and source receipt time are one atomic snapshot.
                    self.latest_frame = frame
                    self.latest_decoded_time = received
                    self.detections = dets
                    self.det_frame_time = received
                    self.result_snapshot = (frame, tuple(dets), received)
                    self.vision_last_error = ''
            except Exception as error:
                with self.frame_lock:
                    self.result_snapshot = None
                    self.det_frame_time = 0.0
                    self.vision_last_error = str(error)
                self.get_logger().error(f'영상 디코딩/YOLO 오류: {error}', throttle_duration_sec=5.0)

    def report_vision(self, reason):
        if reason == self.last_vision_report:
            return
        self.last_vision_report = reason
        if reason:
            self.get_logger().warn(reason)
        else:
            self.get_logger().info('영상 수신·추론 복구: 다른 안전 조건 충족 시 재개')

    def detect_callback(self):
        """[메인] 저장된 검출 결과 + 최신 라이다로 정지 여부를 판단한다. 가벼워서 빠르게 돈다."""
        now = time.monotonic()

        with self.frame_lock:
            snapshot = self.result_snapshot
            rx_time = self.last_frame_time
            error = self.vision_last_error
        self.camera_stale = now-rx_time > self.frame_timeout
        self.inference_stale = snapshot is None or now-snapshot[2] >= self.frame_timeout
        if self.camera_stale:
            self.stop_robot()
            self.report_vision(f'{self.image_topic} 수신 콜백 지연(1초 초과) — 안전정지')
            return
        if self.inference_stale:
            self.stop_robot()
            self.report_vision('영상 수신 중 / 디코딩·추론 결과 지연 — 안전정지' + (f': {error}' if error else ''))
            return
        self.report_vision('')
        source_frame, dets, source_time = snapshot
        frame = source_frame.copy() if self.show_window else None
        img_w = source_frame.shape[1]

        detected = False
        for (x1, y1, x2, y2, conf, name) in dets:
            is_target = (not self.estop_classes) or (name in self.estop_classes)
            obj_dist = 99.9
            if is_target and self.last_scan is not None:
                box_cx = (x1 + x2) / 2.0
                hfov = math.radians(60.0)
                # 화면 오른쪽 = 로봇 기준 시계방향 = 음수 각도
                angle_offset = -(((box_cx - (img_w / 2.0)) / (img_w / 2.0)) * (hfov / 2.0))
                total_samples = len(self.last_scan.ranges)
                if total_samples > 0:
                    idx = int(((angle_offset - self.last_scan.angle_min) / self.last_scan.angle_increment) % total_samples)
                    slice_ranges = [self.last_scan.ranges[(idx + i) % total_samples] for i in range(-5, 6)]
                    obj_dist = get_min_dist(slice_ranges)
                if obj_dist <= self.estop_distance_threshold:
                    detected = True

            if self.show_window:
                hit = is_target and obj_dist <= self.estop_distance_threshold
                color = (0, 0, 255) if hit else (127, 0, 127)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, f'{name} {conf:.2f} {obj_dist:.2f}m', (x1, max(15, y1 - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        was_active = self.estop_active
        self.estop_active, self.last_detect_time = update_estop(
            self.estop_active, detected, now, self.last_detect_time, self.estop_release_delay)

        if self.estop_active and not was_active:
            self.stop_robot()
            self.get_logger().warn('비상정지! 사람 감지 (거리 조건 만족)')
        elif was_active and not self.estop_active:
            self.get_logger().info('사람이 멀어졌습니다: 주행 재개')

        if self.show_window:
            if self.estop_active:
                cv2.putText(frame, 'EMERGENCY STOP', (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)
            self.display_frame = frame

    def show_frame(self):
        """[메인 스레드] 창 띄우기는 반드시 메인 스레드에서 해야 검은 화면이 되지 않는다"""
        if self.show_window and self.display_frame is not None:
            cv2.imshow('Camera Topic', self.display_frame)
            cv2.waitKey(1)

    def close_camera(self):
        self.running = False
        self.frame_event.set()
        if self.yolo_thread is not None:
            self.yolo_thread.join(timeout=2.0)
        if cv2 is not None:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    # ---------- 콜백 ----------
    def map_callback(self, msg):
        self.stop_robot()
        q = msg.info.origin.orientation
        map_yaw = atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2 * (q.y*q.y + q.z*q.z))
        if abs(map_yaw) > 1e-6:
            self.cancel_goal()
            self.blocked = None
            self.base_blocked = None
            self.get_logger().error('origin yaw=0 지도만 지원합니다')
            return
        self.path_xy = []
        self.need_plan = self.goal_pose is not None
        self.map_resolution = msg.info.resolution
        self.map_origin = [msg.info.origin.position.x, msg.info.origin.position.y]
        self.map_data = np.array(msg.data).reshape((msg.info.height, msg.info.width))

        inflate_cells = int(math.ceil(self.robot_radius / self.map_resolution))
        self.base_blocked = make_blocked_grid(self.map_data, inflate_cells)
        self.blocked = self.base_blocked.copy()
        self.cost_grid = clearance_cost(self.blocked, self.map_resolution,
                                        self.clearance_margin, self.clearance_weight)
        self.get_logger().info(f'Map Received: {msg.info.width} x {msg.info.height}')

    def pose_callback(self, msg):
        self.current_pose = [msg.pose.pose.position.x, msg.pose.pose.position.y]
        q = msg.pose.pose.orientation
        self.current_yaw = atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))

    def goal_callback(self, msg):
        self.stop_robot()           # 새 목표를 받으면 일단 정지
        if msg.header.frame_id not in ('', 'map'):
            self.cancel_goal()
            self.get_logger().error('목표는 map 좌표계로 보내세요')
            return
        self.avoiding = False
        self.turning = False
        self.clear_since = None
        self.path_xy = []           # 이전 경로는 버린다
        self.goal_pose = [msg.pose.position.x, msg.pose.position.y]
        self.need_plan = True
        # 새 목표를 받으면 이전에 표시한 임시 장애물은 지운다 (물체가 치워졌을 수도 있으므로)
        if self.base_blocked is not None:
            self.blocked = self.base_blocked.copy()
        self.get_logger().info(f'New Goal: {self.goal_pose}')

    def scan_callback(self, msg):
        self.last_scan = msg
        self.scan_time = time.monotonic()
        # Assumes scan frame is aligned with base heading, as on the original TB3.
        self.front_dist = scan_sector(msg, 0.0, math.radians(30))
        self.left_dist = scan_sector(msg, math.radians(60), math.radians(25))
        self.right_dist = scan_sector(msg, math.radians(-60), math.radians(25))
        self.all_dist = scan_sector(msg, 0.0, pi)

    # ---------- 경로 계획 ----------
    def plan(self, is_replan=False):
        self.need_plan = False
        self.stop_robot()  # A* may take time: never retain an old nonzero command.
        self.path_xy = []
        if self.blocked is None or self.goal_pose is None:
            self.get_logger().warn('아직 지도를 못 받았습니다')
            return False
        if self.current_pose is None:
            self.get_logger().warn('아직 /amcl_pose를 못 받았습니다 (2D Pose Estimate를 찍었나요?)')
            return False

        start = world_to_grid(self.current_pose[0], self.current_pose[1],
                              self.map_origin, self.map_resolution)
        end = world_to_grid(self.goal_pose[0], self.goal_pose[1],
                            self.map_origin, self.map_resolution)

        height, width = self.blocked.shape
        if not (0 <= end[0] < height and 0 <= end[1] < width) or self.blocked[end[0]][end[1]]:
            self.get_logger().warn('목표가 벽 근처이거나 지도 밖/미확인 구역입니다. 다른 곳을 찍어주세요')
            if not is_replan:
                self.cancel_goal()
            return False

        # 로봇이 부풀린 벽 안쪽에 서 있으면 가장 가까운 빈 칸을 시작점으로 쓴다
        # Do not teleport the start out of inflated walls: that can cut a corner.
        sr, sc = start
        if not (0 <= sr < height and 0 <= sc < width) or self.blocked[sr, sc]:
            self.get_logger().warn('현재 위치가 벽 여유 영역 안입니다. 초기 위치/실제 여유를 확인하세요')
            self.publish_path()
            return False
        self.cost_grid = clearance_cost(self.blocked, self.map_resolution,
                                        self.clearance_margin, self.clearance_weight)
        cells = run_astar(self.blocked, start, end, self.cost_grid)
        if cells is None:
            self.get_logger().warn('경로를 찾지 못했습니다')
            if not is_replan:
                self.cancel_goal()
            return False

        self.path_xy = [grid_to_world(r, c, self.map_origin, self.map_resolution) for r, c in cells]
        self.path_idx = 0
        self.get_logger().info(f'경로 생성: {len(self.path_xy)}칸 (약 {len(self.path_xy) * self.map_resolution:.1f}m)')
        self.publish_path()
        return True

    def stop_robot(self):
        # Emergency/goal/map stops bypass all smoothing.
        with self.command_lock:
            self.last_cmd_v = 0.0
            self.last_cmd_w = 0.0
            self.last_cmd_time = time.monotonic()
            self.cmd_pub.publish(Twist())

    def motion_blocked(self):
        now = time.monotonic()
        if self.closing or self.estop_active or self.vision_fault:
            return True
        if now - self.scan_time > self.scan_timeout or self.all_dist <= self.stop_dist:
            return True
        if self.estop_enabled:
            return (self.camera_stale or self.inference_stale or
                    now - self.last_frame_time > self.frame_timeout or
                    now - self.det_frame_time > self.frame_timeout)
        return False

    def publish_smooth(self, v, w, stationary=False):
        with self.command_lock:
            if self.motion_blocked():
                self.stop_robot()
                return
            now = time.monotonic()
            dt = max(0.001, min(0.10, now - self.last_cmd_time))
            self.last_cmd_time = now
            self.last_cmd_v = (0.0 if stationary else slew(
                self.last_cmd_v, max(0.0, min(self.linear_speed, v)),
                self.linear_accel, self.linear_decel, dt))
            self.last_cmd_w = slew(self.last_cmd_w,
                max(-self.max_angular, min(self.max_angular, w)),
                self.angular_accel, self.angular_decel, dt)
            # Check the actual rate-limited arc, not only its lookahead chord.
            if self.current_pose is not None and self.blocked is not None:
                x, y = self.current_pose
                heading = self.current_yaw
                for _ in range(10):
                    nx = x + self.last_cmd_v * cos(heading) * 0.05
                    ny = y + self.last_cmd_v * sin(heading) * 0.05
                    if not segment_clear(self.blocked, (x, y), (nx, ny),
                                         self.map_origin, self.map_resolution):
                        self.stop_robot()
                        return
                    x, y = nx, ny
                    heading += self.last_cmd_w * 0.05
            cmd = Twist()
            cmd.linear.x = self.last_cmd_v
            cmd.angular.z = self.last_cmd_w
            self.cmd_pub.publish(cmd)

    def cancel_goal(self):
        """정지하고 목표와 경로를 지운다 (RViz의 경로선도 사라짐)"""
        self.stop_robot()
        self.goal_pose = None
        self.path_xy = []
        self.publish_path()

    def publish_path(self):
        msg = Path()
        msg.header.frame_id = 'map'
        msg.header.stamp = self.get_clock().now().to_msg()
        for x, y in self.path_xy:
            p = PoseStamped()
            p.header = msg.header
            p.pose.position.x = x
            p.pose.position.y = y
            p.pose.orientation.w = 1.0
            msg.poses.append(p)
        self.path_pub.publish(msg)

    # ---------- 장애물 회피 ----------
    def avoid_obstacle(self):
        now = time.monotonic()
        entered = not self.avoiding
        if entered:
            self.avoiding = True
            self.clear_since = None
            self.avoid_direction = 1.0 if self.left_dist >= self.right_dist else -1.0
            self.stop_robot()
        # Rebuild from static map so removed objects do not accumulate forever.
        if entered or (not self.path_xy and now - self.last_replan_time >= self.replan_cooldown):
            self.last_replan_time = now
            self.blocked = self.base_blocked.copy()
            mark_scan_obstacles(self.blocked, self.last_scan.ranges,
                self.last_scan.angle_min, self.last_scan.angle_increment,
                self.current_pose, self.current_yaw, self.map_origin,
                self.map_resolution, int(math.ceil(self.robot_radius/self.map_resolution)))
            if not self.plan(is_replan=True):
                self.stop_robot()
                return True
        if self.front_dist >= self.obstacle_release:
            if self.clear_since is None:
                self.clear_since = now
            if now - self.clear_since >= self.release_hold and self.path_xy:
                self.avoiding = False
                self.turning = False
                self.clear_since = None
                return False
        else:
            self.clear_since = None
        # Keep one chosen direction for this avoidance episode; never alternate
        # left/right from tiny range changes. Hard stop checks still take priority.
        self.publish_smooth(0.0, self.avoid_direction * self.avoid_turn, stationary=True)
        return True

    def tracking_target(self):
        candidate, self.path_idx = pick_lookahead(
            self.path_xy, self.current_pose, self.lookahead, self.path_idx)
        # Shorten lookahead if the chord crosses an inflated wall.
        if segment_clear(self.blocked, self.current_pose, candidate,
                         self.map_origin, self.map_resolution):
            return candidate
        best = None
        for point in self.path_xy[self.path_idx:]:
            if not segment_clear(self.blocked, self.current_pose, point,
                                 self.map_origin, self.map_resolution):
                break
            if math.hypot(point[0]-self.current_pose[0], point[1]-self.current_pose[1]) > 0.03:
                best = point
            if point == candidate:
                break
        return best

    # ---------- 주행 (Pure Pursuit) ----------
    def control_loop(self):
        if self.motion_blocked():
            self.stop_robot()
            return
        if self.need_plan and self.goal_pose is not None:
            if self.blocked is None or self.current_pose is None:
                self.stop_robot()
                return
            if not self.plan():
                return
        if self.current_pose is None or self.goal_pose is None or self.blocked is None:
            self.stop_robot()
            return
        dist_goal = math.hypot(self.goal_pose[0]-self.current_pose[0],
                               self.goal_pose[1]-self.current_pose[1])
        if dist_goal < self.goal_tolerance:
            self.get_logger().info('목표 도착!')
            self.cancel_goal()
            self.avoiding = False
            self.turning = False
            return
        if self.avoiding or self.front_dist < self.obs_threshold:
            if self.avoid_obstacle():
                return
        if not self.path_xy:
            self.stop_robot()
            return
        target = self.tracking_target()
        if target is None:
            self.stop_robot()
            now = time.monotonic()
            if now - self.last_replan_time >= self.replan_cooldown:
                self.last_replan_time = now
                self.plan(is_replan=True)
            return
        _, alpha = calculate_velocity(self.current_pose, self.current_yaw,
                                       target, self.linear_speed)
        if self.turning:
            self.turning = abs(alpha) > self.turn_exit_angle
        elif abs(alpha) > self.turn_in_place_angle:
            self.turning = True
        if self.turning:
            desired_w = max(-self.turn_speed, min(self.turn_speed, 0.7*alpha))
            self.publish_smooth(0.0, desired_w, stationary=True)
            return
        # Slow down near the goal, a turn, or a frontal obstacle.
        goal_scale = min(1.0, max(0.25, (dist_goal-self.goal_tolerance)/self.goal_slow_distance))
        turn_scale = max(0.35, cos(alpha)**2)
        obstacle_scale = min(1.0, max(0.3,
            (self.front_dist-self.stop_dist)/(0.60-self.stop_dist)))
        v = self.linear_speed * min(goal_scale, turn_scale, obstacle_scale)
        angular, _ = calculate_velocity(self.current_pose, self.current_yaw, target, v)
        # Respect angular limit by slowing linear motion too (preserve curvature).
        if abs(angular) > self.max_angular:
            scale = self.max_angular / abs(angular)
            v *= scale
            angular *= scale
        self.publish_smooth(v, angular)



# --- Temporary person-triggered observation/acknowledgment test ---
import json
import uuid
import urllib.request
from pathlib import Path as FilePath
from std_msgs.msg import String, Int32


