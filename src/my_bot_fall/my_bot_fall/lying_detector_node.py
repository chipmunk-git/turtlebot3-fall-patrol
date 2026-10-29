#!/usr/bin/env python3
"""
터틀봇3 와플파이용 누움 / 쓰러짐 판정 ROS 2 노드 (Remote PC에서 실행)

입력
  /image_raw/compressed   카메라 영상 (터틀봇에서 압축해서 보냄)
  /scan                   라이다
  /amcl_pose              로봇 위치 (없어도 동작, 있으면 사람의 지도 좌표 계산)
  /camera_info            있으면 fx, fy를 자동으로 읽음
출력
  /pose_state             판정 결과 JSON (std_msgs/String)
  /lying_detector/debug/compressed   확인용 영상 (rqt_image_view로 보기)

파일 하나로 동작해요 (판정 규칙, 쓰러짐 감지, 회전 검출이 모두 들어 있음).

실행 (Remote PC):
  ros2 launch fall_detector lying_detector.launch.py
  설정은 config/params.yaml에서 바꿔요 (영상 토픽 이름, 카메라 높이, 모델 파일 등)

/pose_state 예시 (사람마다 한 줄):
  {"people": [{"id": 1, "label": "lying", "decision": "lying", "dist": 1.62, "dist_src": "lidar",
               "bearing_deg": 4.1, "map_x": 2.31, "map_y": -0.42, "votes_lying": 18, "votes_total": 22,
               "lying_sec": 6.2, "motion": 0.004, "fall": false, ...}],
   "target": 0, "fall_image": null}
  - label:    이번 프레임 판정 (15프레임 다수결)
  - decision: 1.2~3m 안에서 여러 각도로 모은 투표 결과 (lying / not_lying / pending)
  - motion:   초당 관절 이동량 / 박스 높이. 로그용 진단값이며 누움 유지 판정은 차단하지 않음
  - target:   순찰 매니저가 신경 써야 할 사람의 people 번호 (쓰러짐 > 누움 > 가장 가까운 사람)
  - state:    사람의 상태값. normal(정상) / lying(누워 있음, 넘어지는 순간은 못 봄) /
              fell(넘어짐, 갑자기 누운 순간을 봄) / fallen(연속 관찰 중 누움이 fall_after_sec 유지된 의심 상태)
              맨 바깥의 state는 현재 보이는 사람 기준. 안 보이는 기억만 있으면 unknown.
  - state_sec: 누워 있음/넘어짐이 시작된 뒤 지난 시간(초)
"""
import json
import math
import os
import time
import threading
from collections import Counter, defaultdict, deque

import cv2
import numpy as np
import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
import tf2_ros
from rclpy.time import Time
from rclpy.node import Node
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, CompressedImage, Image, LaserScan
from std_msgs.msg import String
from nav_msgs.msg import OccupancyGrid
from ultralytics import YOLO
from ament_index_python.packages import get_package_share_directory


# ===========================================================================
# 판정 규칙
# ===========================================================================
# COCO 17개 관절 번호
HEAD = (0, 1, 2, 3, 4)
LEG = (11, 12, 13, 14, 15, 16)      # 엉덩이, 무릎, 발목
LEG_MIN_SPAN = 0.4                  # 다리 관절이 바닥 높이에서 가로로 이만큼(m) 넘게 퍼져 있으면 누운 다리
L_SH, R_SH, L_HIP, R_HIP = 5, 6, 11, 12
L_KNEE, R_KNEE, L_ANK, R_ANK = 13, 14, 15, 16
SKELETON = [(5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12),
            (11, 12), (11, 13), (13, 15), (12, 14), (14, 16)]

# ---- 판정 기준 ----
KPT_TH = 0.4             # 관절 신뢰도 기준
PERSON_CONF = 0.3        # 사람 검출 신뢰도 기준
DARK_MEAN = 30           # 화면 평균 밝기(0~255)가 이보다 낮으면 사람 검출 결과를 버림 (어두운 화면 전체를 사람으로 잡는 오탐 방지)
BIG_BOX_RATIO = 0.85     # 화면이 조금 어두운데(DIM_MEAN 미만) 이 비율보다 큰 박스이고 관절이 2개 이하면 버림
DIM_MEAN = 50            # 큰 박스 규칙은 이 밝기 미만일 때만 (밝을 때 큰 박스는 0.5m 안의 진짜 사람인 경우가 많음)
LYING_ENTER_DEG = 60     # 몸통이 수직에서 이만큼 기울면 누움 (관절로 판단할 때)
LYING_EXIT_DEG = 45      # 누움 상태에서는 이 아래로 세워야 풀림
BOX_LYING_RATIO = 1.2    # 박스 가로/세로가 이보다 크면 누운 모양
LYING_MAX_H = 0.45       # 머리/몸 꼭대기가 이보다 낮으면 누움 (m)
UPRIGHT_MIN_H = 0.60     # 이보다 높으면 안 누움 (m)
LYING_MIN_WIDTH = 1.0    # 낮은데 가로로 이보다 길면 누움 (m)
EDGE_PX = 3              # 화면 끝에서 이 픽셀 안이면 "잘림"
FILL_TOP_RATIO = 0.10    # 거리를 모를 때: 박스 위 끝이 화면 높이의 이 비율 안이고 아래 끝이 화면 아래면 "꽉 채움" (아주 가까운 발·정강이)
SMOOTH_FRAMES = 15       # 최근 N프레임 다수결
LYING_MIN_FRAMES = 5     # 최근 기록에 누움이 이만큼은 있어야 LYING으로 표시 (잠깐 생겼다 사라지는 오탐 방지)
ROT_CONF = 0.5           # 회전 검출은 오탐이 많아서 더 높은 신뢰도 기준 사용
ROT_MIN_KPTS = 6         # 회전 검출은 관절이 이만큼 이상 보여야 사람으로 인정
SHIN_UPRIGHT_DEG = 30    # 정강이가 수직에서 이 각도 이내면 서 있는 다리
SHIN_MIN_PX = 0.12       # 정강이 길이가 화면 높이의 이 비율 이상일 때만 사용
TALL_CUT_RATIO = 0.8     # 위가 잘린 박스의 가로/세로가 이 이하면 세로로 긴 몸
TALL_CUT_MIN_H = 0.5     # 그 박스 높이가 화면 높이의 이 비율 이상일 때만

# ---- 쓰러짐 감지 기준 ----
FALL_WINDOW = 3.5        # 확실히 서 있음 -> 누움이 이 시간(초) 안에 일어나면 쓰러짐 (처리 속도가 느린 PC 고려)
FALL_CONFIRM = 2         # 누움이 이 프레임 수 이상, 그리고
FALL_CONFIRM_SEC = 0.8   # 이 시간(초) 이상 이어져야 인정 (처리 속도가 느려도 같은 기준이 되게 시간으로)
FALL_UPRIGHT_MIN = 2     # 그 직전에 '확실히 서 있음'이 이만큼 이상 있어야 함 (시점 변화로 인한 오탐 방지)
FEET_TRUST_MIN = 1.0     # 라이다 없이 발 위치로만 잰 거리가 이보다 가까우면 믿지 않음
UPRIGHT_STRONG_H = 0.8   # 머리나 몸 꼭대기가 이 높이(m) 이상이면 '확실히 서 있음'
LIDAR_FLOOR = 0.2        # 라이다 값이 이보다 가까우면 잡음으로 보고 버림 (m)
FALL_SAME_PLACE = 0.15   # 서 있던 사람의 가로 위치가 누운 사람 박스 가로 범위에서 화면 폭의 이 비율 안이면 같은 사람으로 봄
FALL_SHOW_SEC = 3.0      # 화면에 FALL 표시를 유지할 시간

LABEL_EN = {"lying": "LYING", "not_lying": "NOT LYING", "too_close": "TOO CLOSE", "unknown": "UNKNOWN"}
LABEL_KO = {"lying": "누워 있음", "not_lying": "누워 있지 않음",
            "too_close": "너무 가까움 (뒤로 물러나야 판단 가능)", "unknown": "판단 불가"}
COLORS = {"lying": (48, 90, 216), "not_lying": (117, 158, 29),
          "too_close": (0, 200, 255), "unknown": (160, 160, 160)}
VOTE_LABELS = ("lying", "not_lying")
TARGET_RADIUS = 1.2
LYING_RADIUS = 2.0                  # 누운 사람은 몸 길이만큼 위치가 달라 보일 수 있음 (다리 쪽 / 머리 쪽)      # 지도 좌표가 이 거리(m) 안이고
TARGET_KEEP_SEC = 60.0   # 마지막으로 본 지 이 시간(초) 안이면 같은 사람(같은 번호)으로 봄
STATE_EN = {"normal": "NORMAL", "lying": "LYING", "fell": "FELL!", "fallen": "FALLEN!!"}
STATE_ORDER = {"fallen": 4, "fell": 3, "lying": 2, "normal": 1}
LIDAR_OBJ_GAP = 0.15     # 라이다 점 사이가 이보다 멀면 다른 물체로 나눔 (m)



def angle_from_vertical(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    return math.degrees(math.atan2(abs(dx), abs(dy) + 1e-6))


class Camera:
    """수평(또는 tilt만큼 위로 기울인) 카메라의 픽셀 <-> 실제 거리/높이 변환"""

    def __init__(self, height_m, fy, tilt_deg, vfov_deg):
        self.cam_h = height_m
        self.fy_arg = fy
        self.tilt = math.radians(tilt_deg)
        self.vfov = math.radians(vfov_deg)
        self.fy = None
        self.cy = None
        self.fx = None
        self.cx = None
        self.image_size = None
        self.calibration = None

    def set_calibration(self, width, height, k):
        if width > 0 and height > 0 and k[0] > 0 and k[4] > 0:
            self.calibration = (width, height, list(k))
            self.image_size = None

    def setup(self, w, h):
        if self.image_size == (w, h):
            return
        self.image_size = (w, h)
        self.cx, self.cy = w / 2, h / 2
        self.fy = self.fy_arg or (h / 2) / math.tan(self.vfov / 2)
        self.fx = self.fy
        if self.calibration is not None and not self.fy_arg:
            iw, ih, k = self.calibration
            self.fx, self.fy = k[0] * w / iw, k[4] * h / ih
            self.cx, self.cy = k[2] * w / iw, k[5] * h / ih

    def ray(self, y):
        return self.tilt + math.atan((self.cy - y) / self.fy)

    def dist_from_feet(self, y_bottom):
        a = self.ray(y_bottom)
        if a >= -math.radians(1):
            return None
        return self.cam_h / math.tan(-a)

    def height_at(self, y, dist):
        return self.cam_h + dist * math.tan(self.ray(y))

    def width_m(self, px, dist):
        return px * dist / self.fx

    def horizon_y(self):
        return self.cy + self.fy * math.tan(self.tilt)


def ok_kpt(kxy, kconf, i, w, h):
    x, y = kxy[i]
    return kconf[i] >= KPT_TH and 0 <= x < w and 0 <= y < h


def measure(box, kxy, kconf, w, h, cam, fixed_dist, dist_src="lidar"):
    """거리, 머리 높이, 꼭대기 높이, 실제 폭 계산 (fixed_dist가 있으면 그 거리를 사용)"""
    x1, y1, x2, y2 = box
    m = {"dist": None, "dist_src": None, "head_h": None, "top_h": None,
         "top_cut": y1 <= EDGE_PX, "side_cut": x1 <= EDGE_PX or x2 >= w - 1 - EDGE_PX,
         "feet_cut": y2 >= h - 1 - EDGE_PX, "width": None,
         "kp_top_h": None, "leg_span": None, "leg_top_h": None}
    if cam is None:
        return m
    if fixed_dist:
        m["dist"], m["dist_src"] = fixed_dist, dist_src
    elif not m["feet_cut"]:
        d = cam.dist_from_feet(y2)
        if d is not None and d < 8:
            m["dist"], m["dist_src"] = d, "feet"
    d = m["dist"]
    if d is None:
        return m
    heads = [kxy[i][1] for i in HEAD if ok_kpt(kxy, kconf, i, w, h)]
    if heads:
        m["head_h"] = cam.height_at(float(np.mean(heads)), d)
    m["top_h"] = cam.height_at(y1, d)
    m["width"] = cam.width_m(x2 - x1, d)
    # 관절 기준 높이: 박스 위쪽은 배경까지 잡히는 경우가 있어서, 실제로 찾은 관절 중 가장 높은 곳도 계산
    good = [i for i in range(len(kxy)) if ok_kpt(kxy, kconf, i, w, h)]
    if good:
        m["kp_top_h"] = cam.height_at(float(min(kxy[i][1] for i in good)), d)
    legs = [i for i in LEG if i in good]
    if len(legs) >= 3:
        xs = [kxy[i][0] for i in legs]
        m["leg_span"] = cam.width_m(max(xs) - min(xs), d)
        m["leg_top_h"] = cam.height_at(float(min(kxy[i][1] for i in legs)), d)
    return m


def judge_geo(m):
    """실제 높이/폭으로 판정. (라벨 또는 None, 근거)"""
    if m["dist"] is None:
        return None, "거리 모름"
    if m["head_h"] is not None:
        if m["head_h"] <= LYING_MAX_H:
            return "lying", f"머리 높이 {m['head_h']:.2f}m"
        if m["head_h"] >= UPRIGHT_MIN_H:
            return "not_lying", f"머리 높이 {m['head_h']:.2f}m"
    # 머리는 안 보이고 다리만 보일 때: 다리 관절이 모두 바닥 높이이고 가로로 퍼져 있으면 누운 다리
    if (m["head_h"] is None and m["leg_span"] is not None and m["leg_top_h"] <= LYING_MAX_H
            and m["leg_span"] >= LEG_MIN_SPAN and (m["kp_top_h"] is None or m["kp_top_h"] <= LYING_MAX_H)):
        return "lying", f"다리가 바닥에 가로로 {m['leg_span']:.2f}m"
    if m["top_cut"]:
        if m["top_h"] >= UPRIGHT_MIN_H:
            return "not_lying", f"위가 잘림, 최소 {m['top_h']:.2f}m 이상"
        # 화면 위가 잘렸다는 이유만으로 '너무 가까움'을 반환하지 않는다.
        # 관절 판정으로 넘기고, 아래의 실제 근거리+잘림 검사에서만 판단한다.
        return None, f"위가 잘림 (화면 위 높이 {m['top_h']:.2f}m)"
    if m["top_h"] <= LYING_MAX_H:
        extra = f", 가로 {m['width']:.2f}m" if m["width"] >= LYING_MIN_WIDTH else ""
        return "lying", f"몸 꼭대기 {m['top_h']:.2f}m{extra}"
    if m["top_h"] >= UPRIGHT_MIN_H:
        return "not_lying", f"몸 꼭대기 {m['top_h']:.2f}m"
    if m["width"] >= LYING_MIN_WIDTH:
        return "lying", f"낮고({m['top_h']:.2f}m) 가로로 김({m['width']:.2f}m)"
    return None, f"애매한 높이 {m['top_h']:.2f}m"


def judge_joint(box, kxy, kconf, w, h, prev):
    """거리를 모를 때: 몸통 각도 -> 정강이 방향 -> 박스 모양 순서로 판정"""
    x1, y1, x2, y2 = box
    ratio = (x2 - x1) / max(y2 - y1, 1e-6)
    shoulders = [i for i in (L_SH, R_SH) if ok_kpt(kxy, kconf, i, w, h)]
    hips = [i for i in (L_HIP, R_HIP) if ok_kpt(kxy, kconf, i, w, h)]
    if shoulders and hips:
        sh_mid = np.mean([kxy[i] for i in shoulders], axis=0)
        hip_mid = np.mean([kxy[i] for i in hips], axis=0)
        torso = angle_from_vertical(sh_mid, hip_mid)
        th = LYING_EXIT_DEG if prev == "lying" else LYING_ENTER_DEG
        return ("lying" if torso >= th else "not_lying"), f"몸통 각도 {torso:.0f}°"

    # 다리만 보일 때: 정강이(무릎->발목)가 세로로 길게 서 있으면 서 있는 사람
    shins = []
    for knee, ank in ((L_KNEE, L_ANK), (R_KNEE, R_ANK)):
        if ok_kpt(kxy, kconf, knee, w, h) and ok_kpt(kxy, kconf, ank, w, h):
            k, a = kxy[knee], kxy[ank]
            if a[1] > k[1] and (a[1] - k[1]) >= SHIN_MIN_PX * h:   # 발목이 무릎보다 아래
                shins.append(angle_from_vertical(k, a))
    if shins and min(shins) <= SHIN_UPRIGHT_DEG:
        return "not_lying", f"정강이가 세로 ({min(shins):.0f}°)"

    # 박스가 화면 위에서 잘렸고 세로로 길면: 낮은 카메라에서 누운 사람은 위가 잘리지 않음
    if y1 <= EDGE_PX and ratio <= TALL_CUT_RATIO and (y2 - y1) >= TALL_CUT_MIN_H * h:
        return "not_lying", f"위가 잘린 세로 박스 (비율 {ratio:.2f})"

    # 박스 비율로 누움을 판단하려면 상체(머리·어깨·엉덩이) 중 하나는 보여야 하고
    # 위가 잘리지 않아야 함. (발만 나란히 보이면 박스가 가로로 넓어서 누운 걸로 착각함)
    upper = any(ok_kpt(kxy, kconf, i, w, h) for i in HEAD + (L_SH, R_SH, L_HIP, R_HIP))
    if not upper:
        return "unknown", "상체 관절이 안 보임 (다리/발만)"
    if y1 <= EDGE_PX:
        return "unknown", "위가 잘림"
    if ratio >= BOX_LYING_RATIO:
        return "lying", f"박스 비율 {ratio:.2f}"
    return "unknown", "몸 일부만 보임"


def eligible_partial_lying(box, measurement, n_kpt, dist, dist_src, raw,
                           low_top_height=0.55, enabled=True):
    """상체 관절 없어도 부분 신체가 누운 것으로 관찰할 최소 조건.

    이미 YOLO가 누운 자세(lying)라고 판단한 경우에만 사용하며,
    짧은 세로 박스·높게 보이는 다리·근거리 불신 거리·관절 부족은 제외한다.
    """
    if not enabled or raw != "lying" or n_kpt < 3:
        return False
    if dist is None or not math.isfinite(dist) or (dist_src == "feet" and dist < FEET_TRUST_MIN):
        return False
    top_h = measurement.get("top_h")
    if top_h is None or not math.isfinite(top_h) or top_h > low_top_height:
        return False
    if measurement.get("top_cut"):
        return False
    bw = max(float(box[2]-box[0]), 0.0)
    bh = max(float(box[3]-box[1]), 1.0)
    return bw / bh >= BOX_LYING_RATIO


def judge_partial_pose(m, kxy, kconf, w, h, cam):
    """상·하체가 함께 보이지 않을 때 바닥에 가까운 연결 관절만으로 보수적으로 누움을 추정.

    - 3개 이상의 신뢰 가능한 관절, 실제 거리, 바닥과 가까운 높이, 가로 방향을 모두 요구.
    - 어깨+골반이 둘 다 보이는 일반적인 자세는 기존 몸통 판정에 맡김.
    - 단독 머리·손목·발목, 거리 미확인, 위가 잘린 서 있는 사람은 추측하지 않음.
    """
    dist = m["dist"]
    if dist is None or not math.isfinite(dist) or dist <= 0:
        return None
    if m["top_cut"] and m["top_h"] is not None and m["top_h"] >= UPRIGHT_MIN_H:
        return None
    present = {i for i in range(len(kxy)) if ok_kpt(kxy, kconf, i, w, h)}
    if len(present) < 3:
        return None
    if present.intersection((L_SH, R_SH)) and present.intersection((L_HIP, R_HIP)):
        return None

    max_height = 0.50       # 원본 LYING_MAX_H(0.45m)보다 조금 완화하되 바닥 가까운 경우만
    max_y_spread = 0.24    # 연결된 관절이 서로 높게 벌어지면 누움 근거로 사용하지 않음
    groups = []
    # 머리 1개 이상 + 어깨 1개 이상, 합계 3개 이상
    heads = present.intersection(HEAD)
    shoulders = present.intersection((L_SH, R_SH))
    if heads and shoulders and len(heads | shoulders) >= 3:
        groups.append(("머리·어깨", sorted(heads | shoulders), 0.16))
    # 한쪽 팔이나 한쪽 다리가 이어져 있어야 함 (멀리 떨어진 임의 관절 3개 조합 방지)
    for name, chain, min_span in (
        ("왼팔", (5, 7, 9), 0.30), ("오른팔", (6, 8, 10), 0.30),
        ("왼다리", (11, 13, 15), 0.32), ("오른다리", (12, 14, 16), 0.32),
    ):
        if all(i in present for i in chain):
            groups.append((name, chain, min_span))

    for name, ids, min_span in groups:
        heights = [cam.height_at(float(kxy[i][1]), dist) for i in ids]
        if not all(math.isfinite(z) for z in heights):
            continue
        if max(heights) > max_height or min(heights) < -0.10:
            continue
        if max(heights) - min(heights) > max_y_spread:
            continue
        xs = [float(kxy[i][0]) for i in ids]
        span = cam.width_m(max(xs) - min(xs), dist)
        if span >= min_span:
            return "lying", f"부분 신체({name}) 바닥 근처 수평 {span:.2f}m"
    return None


def judge(box, kxy, kconf, w, h, cam, fixed_dist, prev, dist_src="lidar", partial_body_enabled=True):
    m = measure(box, kxy, kconf, w, h, cam, fixed_dist, dist_src)
    # 거리를 못 받았는데(라이다 없음·버림) 발 위치로 1m 안이 나오면: 몸 일부 박스라 거리가 틀렸을 가능성이 큼.
    # 이 거리로 높이를 계산하면 가까이 선 사람 다리를 '몸 꼭대기 0.4m -> 누움'으로 잘못 봄 (10/26 실제 영상) -> 판단 안 함
    # 발 위치 기반 1m 미만 거리값은 신뢰하지 않는다. 그러나 '너무 가까움'으로
    # 강제 분류하지 말고, 거리 없이도 가능한 관절 방향 판정을 시도한다.
    # 이 경로는 높이 추정(거리 의존)에는 사용하지 않으므로 앉은/선 사람 다리가
    # 낮게 추정되어 누움으로 오인되는 문제를 피한다.
    if not fixed_dist and m["dist"] is not None and m["dist"] < FEET_TRUST_MIN:
        label, reason = judge_joint(box, kxy, kconf, w, h, prev)
        return label, f"발 위치 추정 {m['dist']:.2f}m는 신뢰 불가 -> {reason}", m
    # 근거리에서 어깨와 골반을 동시에 보기 어려운 경우: 독립된 부분 신체 근거로 보완
    partial = judge_partial_pose(m, kxy, kconf, w, h, cam) if partial_body_enabled else None
    if partial is not None:
        return partial[0], partial[1], m
    label, why = judge_geo(m)
    if label is None:
        label, why2 = judge_joint(box, kxy, kconf, w, h, prev)
        why = f"{why} -> {why2}"
    return label, why, m


def strong_low_horizontal_upper(box, kxy, kconf, w, h, m, top_limit=0.55, min_width=0.80):
    """누운 상체가 낮고 수평으로 길게 찍힌 강한 근거를 관절 부족 시 보완.

    머리(코/눈/귀) 한 점이 *실제로* 검출되고, 거리와 높이·박스가 동시에
    뒷받침될 때만 인정한다. 다리/발만 찍힌 영상은 절대로 통과하지 않는다.
    """
    d = m.get("dist")
    if d is None or not math.isfinite(d) or d <= 0:
        return False
    if m.get("dist_src") == "feet" and d < FEET_TRUST_MIN:
        return False
    if m.get("top_cut") or m.get("feet_cut"):
        return False
    if m.get("top_h") is None or not (-0.10 <= m["top_h"] <= top_limit):
        return False
    if m.get("width") is None or m["width"] < min_width:
        return False
    ratio = (box[2]-box[0])/max(box[3]-box[1], 1.0)
    if ratio < 1.65:
        return False
    heads = [i for i in HEAD if ok_kpt(kxy, kconf, i, w, h)]
    return bool(heads and m.get("head_h") is not None and
                -0.1 <= m["head_h"] <= top_limit)



def evidence_for_upper_lying(box, kxy, kconf, w, h, m,
                             top_limit=0.55, min_width=0.80):
    """상체 누움의 신뢰 가능한 증거인지 확인 (원시 lying 판단과 별개).

    머리·어깨 관절이 흐려도 *연결된 팔꿈치-손목*이 확실히 검출된 경우에는
    낮은 높이, 수평 몸 박스, 거리 신뢰도를 함께 확인해 유효 관찰을 허용한다.
    골반·무릎·발목만으로는 증거를 만들지 않아 다리만 보일 때의 오탐을 막는다.
    """
    d = m.get("dist")
    if d is None or not math.isfinite(d) or d <= 0:
        return False, "거리 없음"
    if m.get("dist_src") == "feet" and d < FEET_TRUST_MIN:
        return False, "가까운 발 거리 추정 불신"
    low = any(z is not None and math.isfinite(z) and -0.10 <= z <= top_limit
              for z in (m.get("head_h"), m.get("kp_top_h")))
    head_or_shoulder = any(ok_kpt(kxy, kconf, i, w, h)
                           for i in HEAD + (L_SH, R_SH))
    ratio = (float(box[2])-float(box[0]))/max(float(box[3])-float(box[1]),1.0)
    wide_image = ratio >= 1.65
    if head_or_shoulder and (low or (m.get("dist") is None and wide_image)):
        return True, "머리/어깨 + 낮은 상체"

    # 얼굴·어깨가 검출되지 않은 실제 누움(사진 1)을 위한 대체 근거.
    # 바운딩박스만으로 신체 부위를 단정하지 않고 팔꿈치→손목 연결을 요구한다.
    if (m.get("top_cut") or m.get("feet_cut") or
            m.get("top_h") is None or not -0.10 <= m["top_h"] <= top_limit or
            m.get("width") is None or m["width"] < min_width or not wide_image):
        return False, "수평 상체 보조 조건 부족"
    for elbow, wrist in ((7, 9), (8, 10)):
        if not (ok_kpt(kxy, kconf, elbow, w, h) and
                ok_kpt(kxy, kconf, wrist, w, h)):
            continue
        # 다리만 보이는 상황에서 잘못 연결된 작은 점을 상체로 인정하지 않기 위한 검증.
        x1,y1=kxy[elbow]; x2,y2=kxy[wrist]
        if (m["width"]*abs(float(x2-x1))/max(float(box[2]-box[0]),1.0) < 0.18 or
                abs(float(y2-y1))/max(float(box[3]-box[1]),1.0) > 0.32):
            continue
        if m.get("kp_top_h") is not None and -0.10 <= m["kp_top_h"] <= top_limit:
            return True, "낮고 수평인 팔꿈치-손목 + 상체형 박스"
    return False, "확실한 머리/어깨/팔 관절 근거 없음"


def retain_partial_suspicion(target, raw, n_kpt, now, tolerance_sec, moving=False):
    """직전 누움 근거가 있는 같은 대상에서 관절 두 개 프레임은 의심만 보존.

    raw는 그대로 unknown. 투표나 최종 낙상 유효 시간에는 포함하지 않는다.
    """
    evidence = getattr(target, "last_lying_evidence", None) if target is not None else None
    return bool(target is not None and target.down() and raw == "unknown" and
                n_kpt == 2 and evidence is not None and
                0 <= now - evidence <= tolerance_sec and not moving)


def genuinely_too_close(box, m, n_kpt, dist, close_dist, h, lidar_present=False):
    """자세 판정용 '너무 가까움': 거리 단독이 아닌, 극근거리 + 심한 잘림/관절 부족.

    A* 주행의 LiDAR 장애물 회피·긴급 정지와는 별개이며 이를 해제하지 않는다.
    누운 자세 근거를 충분히 확보한 프레임(관절 3개 이상)은 근거리여도 판정한다.
    """
    if n_kpt >= 3:
        return False
    # 관절 0개+LiDAR 근거도 없으면 카메라 허위 박스를 가까운 사람으로 만들지 않음.
    if n_kpt == 0 and not lidar_present:
        return False
    edge_cut = bool(m["top_cut"] or m["feet_cut"] or m["side_cut"])
    if dist is not None and math.isfinite(dist):
        return dist < close_dist and edge_cut
    # 거리 자체도 모를 때는 화면 전체를 가득 채워 신체가 잘린 경우만 인정.
    x1, y1, x2, y2 = box
    fills = (y1 <= h * FILL_TOP_RATIO and y2 >= h - 1 - EDGE_PX)
    return fills


def unpack(res):
    """YOLO 결과 -> [(박스, 추적번호, 관절좌표, 관절신뢰도), ...]"""
    if res.boxes is None or not len(res.boxes) or res.keypoints is None:
        return []
    boxes = res.boxes.xyxy.cpu().numpy()
    ids = (res.boxes.id.int().cpu().tolist() if res.boxes.id is not None
           else list(range(1, len(boxes) + 1)))
    kxys = res.keypoints.xy.cpu().numpy()
    kconfs = (res.keypoints.conf.cpu().numpy() if res.keypoints.conf is not None
              else np.ones(kxys.shape[:2]))
    return list(zip(boxes, ids, kxys, kconfs))


def overlap_small(a, b):
    """두 박스가 겹친 넓이 / 작은 박스 넓이 (한 박스가 다른 박스 안에 들어 있으면 1에 가까움)"""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / max(small, 1e-6)


def drop_rot_on_upright(dets):
    """회전 검출(누운 사람) 박스의 관절이 절반 넘게 서 있는 사람(세로로 긴 일반 박스) 안에 있으면 버림.
    서 있는 사람의 발·무릎에 옆 물체(프린터 다리 등)를 이어 '누운 사람'으로 잘못 찾은 경우 (10/26 실제 영상: 가짜 fell)"""
    uprights = [d[0] for d in dets if d[1] < 10000 and (d[0][3] - d[0][1]) > 1.3 * (d[0][2] - d[0][0])]
    if not uprights:
        return dets
    keep = []
    for d in dets:
        if d[1] >= 10000:
            pts = [(x, y) for (x, y), c in zip(d[2], d[3]) if c >= KPT_TH]
            inside = [p for p in pts if any(b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3] for b in uprights)]
            if pts and len(inside) * 2 > len(pts):
                continue
        keep.append(d)
    return keep


def dedupe(dets):
    """같은 사람을 두 번 찾은 경우(몸 일부 박스가 몸 전체 박스 안에 들어 있음) 큰 박스만 남김
    단, 회전 검출로 찾은 누운 사람은 일반 박스 안에 있어도 남김 (서 있는 사람 뒤에 누운 사람이 겹쳐 보이면
    YOLO가 두 사람을 박스 하나로 잡음. 같은 사람이면 회전 검출 단계에서 이미 IoU로 걸러짐)"""
    area = lambda d: (d[0][2] - d[0][0]) * (d[0][3] - d[0][1])  # noqa: E731
    keep = []
    for d in sorted(dets, key=area, reverse=True):
        if all(overlap_small(d[0], k[0]) < 0.6 or (d[1] >= 10000 and k[1] < 10000) for k in keep):
            keep.append(d)
    return keep


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / max(union, 1e-6)


class RotAssist:
    """
    YOLO는 누운 사람을 잘 못 잡아서, 화면을 90° 돌려서 한 번 더 찾아봄.
    (돌리면 누운 사람이 서 있는 사람처럼 보여서 잘 잡힘)
    느려지지 않게 ROT_EVERY 프레임마다 한 번만 하고, 중간 프레임에는 회전 결과를 사용하지 않음(오래된 관찰로 타이머가 진행되는 것 방지).
    """

    def __init__(self, every=3):
        self.every = every
        self.count = 0
        self.cache = []

    def extra(self, model, frame, normal_dets):
        self.count += 1
        if self.every <= 0:
            return []
        if self.every != 1 and self.count % self.every != 1:
            return []  # Never reuse old rotated detections as fresh observation.
        if self.count % self.every == 1 or self.every == 1:
            self.cache = self._detect(model, frame)
        have = [d[0] for d in normal_dets]
        out = []
        for d in self.cache:
            if all(iou(d[0], b) < 0.3 for b in have + [o[0] for o in out]):
                out.append(d)
        return out

    @staticmethod
    def _detect(model, frame):
        h, w = frame.shape[:2]
        found = []
        # (회전 방법, 돌린 화면 좌표 (u, v) -> 원래 화면 좌표 (x, y))
        rots = ((cv2.ROTATE_90_CLOCKWISE, lambda u, v: (v, h - 1 - u)),
                (cv2.ROTATE_90_COUNTERCLOCKWISE, lambda u, v: (w - 1 - v, u)))
        for code, back in rots:
            res = model.predict(cv2.rotate(frame, code), conf=ROT_CONF, imgsz=320, classes=[0], verbose=False)[0]
            for rb, _, rk, rc in unpack(res):
                # 벽, 바닥 무늬를 사람으로 착각하는 경우를 거르기: 관절이 충분하고 어깨나 엉덩이가 보여야 함
                good = rc >= KPT_TH
                if good.sum() < ROT_MIN_KPTS or not (good[[L_SH, R_SH, L_HIP, R_HIP]].any()):
                    continue
                xs, ys = zip(back(rb[0], rb[1]), back(rb[2], rb[3]))
                box = np.array([min(xs), min(ys), max(xs), max(ys)], dtype=np.float32)
                if (box[2] - box[0]) < (box[3] - box[1]):
                    continue          # 원래 화면에서 세로로 긴 건 일반 검출이 이미 잘 잡음
                kxy = np.array([back(u, v) for u, v in rk], dtype=np.float32)
                kxy[rc <= 0] = 0
                if any(iou(box, f[0]) >= 0.3 for f in found):
                    continue
                cx = (box[0] + box[2]) / 2 / w
                pid = 10000 + int(cx * 20)    # 위치로 만든 번호 (같은 자리면 같은 번호)
                found.append((box, pid, kxy, rc))
        return found


class FallDetector:
    """안 누움 -> 누움 전환을 위치 기준으로 추적해서 쓰러짐을 감지"""

    def __init__(self):
        self.upright = deque(maxlen=300)          # (시간, 화면 가로 중심 비율, 추적 번호, 로봇 위치, 거리)
        self.lying_run = defaultdict(int)         # ID별 연속 누움 프레임 수
        self.lying_first = {}                     # ID별 누움이 시작된 시각
        self.fallen_ids = {}                      # ID -> 쓰러짐 감지 시각
        self.last_event = None                    # (시각, 화면 가로 위치): 같은 쓰러짐을 두 번 세지 않게
        self.checked = set()                      # 이번 누움 구간에서 이미 검사한 ID
        self.src_pid = None                       # 방금 감지한 쓰러짐 직전에 서 있던 사람의 추적 번호

    def update(self, pid, raw, cx_ratio, now, strong=True, span=None, robot=(0.0, 0.0, 0.0), moving=False,
               upright_now=(), dist=None):
        """쓰러짐이 새로 감지되면 True. strong = 이번 '안 누움'이 확실한 서 있음인지
        span = 누운 사람 박스의 가로 범위 (x1/폭, x2/폭). 옆으로 쓰러지면 박스 가운데가 크게 옮겨지므로 범위로 비교"""
        if raw == "not_lying":
            if strong:
                self.upright.append((now, cx_ratio, pid, robot, dist))
            self.lying_run[pid] = 0
            self.lying_first.pop(pid, None)
            self.checked.discard(pid)
            return False
        if raw != "lying":
            self.lying_run[pid] = 0
            self.lying_first.pop(pid, None)
            return False

        self.lying_run[pid] += 1
        self.lying_first.setdefault(pid, now)
        start = self.lying_first[pid]
        if (pid in self.fallen_ids or self.lying_run[pid] < FALL_CONFIRM
                or now - start < FALL_CONFIRM_SEC or pid in self.checked):
            return False
        if moving:
            return False                          # 로봇이 최근에 움직였으면 화면 위치 비교를 믿을 수 없음 (나중에 다시 검사)
        lo, hi = span if span else (cx_ratio, cx_ratio)
        # 같은 자리(화면 가로 + 거리 0.7m 안)에 다른 사람이 지금도 서 있으면 쓰러진 게 아님
        # (서 있는 사람 뒤의 누운 사람에 앞사람 거리가 잘못 붙은 경우). 나중에 다시 검사
        if any(p != pid and a <= hi and b >= lo and (dist is None or d is None or abs(d - dist) < 0.7)
               for p, a, b, d in upright_now):
            return False
        self.checked.add(pid)                     # 누움이 시작될 때마다 한 번만 검사

        # 화면 위치로 비교하므로 그동안 로봇이 돌거나 움직였으면 비교하지 않음 (회전 중 오탐 방지)
        # 거리도 비교: 누운 몸의 가장 가까운 점은 서 있던 자리보다 0.4m 넘게 멀 수 없고,
        # 로봇 쪽으로 쓰러지면 키만큼(최대 2m) 가까워질 수 있음
        ups = [(t, p, ud) for t, cx, p, rb, ud in self.upright
               if start - FALL_WINDOW <= t <= start and lo - FALL_SAME_PLACE <= cx <= hi + FALL_SAME_PLACE
               and abs(math.atan2(math.sin(rb[2] - robot[2]), math.cos(rb[2] - robot[2]))) < 0.06
               and math.hypot(rb[0] - robot[0], rb[1] - robot[1]) < 0.1
               and (dist is None or ud is None or -0.4 <= ud - dist <= 2.0)]
        if len(ups) < FALL_UPRIGHT_MIN:
            return False
        # 그 자리에 서 있던 사람: 거리가 가장 비슷한 사람, 같으면 가장 최근
        self.src_pid = min(ups, key=lambda e: (abs(e[2] - dist) if dist is not None and e[2] is not None else 0,
                                               -e[0]))[1]
        self.fallen_ids[pid] = now
        le = self.last_event
        if le and now - le[0] < 10 and lo - FALL_SAME_PLACE <= le[1] <= hi + FALL_SAME_PLACE:
            return False                          # 방금 같은 자리에서 감지한 쓰러짐 (다른 검출 번호)
        self.last_event = (now, cx_ratio)
        return True

    def showing(self, pid, now):
        t = self.fallen_ids.get(pid)
        return t is not None and now - t < FALL_SHOW_SEC


def draw_person(frame, box, kxy, kconf, label, pid, lying_sec, m, fall_on, show_debug, tag=None):
    x1, y1, x2, y2 = map(int, box)
    color = (0, 0, 255) if fall_on else COLORS[label]
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3 if fall_on else 2)
    for a, b in SKELETON:
        if kconf[a] >= KPT_TH and kconf[b] >= KPT_TH:
            cv2.line(frame, tuple(map(int, kxy[a])), tuple(map(int, kxy[b])), (255, 255, 255), 2)
    for (x, y), c in zip(kxy, kconf):
        if c >= KPT_TH:
            cv2.circle(frame, (int(x), int(y)), 3, (0, 200, 255), -1)

    if tag:
        text = f"#{pid} {tag}"
    else:
        text = f"#{pid} {'FALL! ' if fall_on else ''}{LABEL_EN[label]}"
    if m["dist"] is not None:
        text += f"  {m['dist']:.2f}m"
    if lying_sec > 0:
        text += f"  {lying_sec:.0f}s"
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    ty = max(y1, th + 46)
    cv2.rectangle(frame, (x1, ty - th - 10), (x1 + tw + 8, ty), color, -1)
    cv2.putText(frame, text, (x1 + 4, ty - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    if show_debug:
        if m["dist"] is None:
            lines = ["dist: ? (feet cut)" if m["feet_cut"] else "dist: ?"]
        else:
            lines = [f"dist {m['dist']:.2f}m ({m['dist_src']})"]
            if m["head_h"] is not None:
                lines.append(f"head {m['head_h']:.2f}m")
            lines.append(f"top {'>=' if m['top_cut'] else ''}{m['top_h']:.2f}m")
            lines.append(f"width {'>=' if m['side_cut'] else ''}{m['width']:.2f}m")
        yy = min(y2 + 20, frame.shape[0] - 8 - 20 * (len(lines) - 1))
        for ln in lines:
            cv2.putText(frame, ln, (x1, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 4)
            cv2.putText(frame, ln, (x1, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
            yy += 20


# ---------------------------------------------------------------------------
# 거리 계산, 투표, 한 프레임 처리
# ---------------------------------------------------------------------------
def scan_min_range(scan, a_from, a_to):
    """라이다에서 각도 a_from~a_to(라디안, 왼쪽이 +) 사이의 가까운 거리. 없으면 None
    점 하나짜리 튀는 값에 속지 않게, 가장 가까운 값 대신 두 번째로 가까운 값을 씀 (점이 1개면 그 값)"""
    if scan is None or scan.angle_increment == 0:
        return None
    n = len(scan.ranges)
    lo, hi = min(a_from, a_to), max(a_from, a_to)
    vals = []
    a = lo
    step = abs(scan.angle_increment)
    floor = max(scan.range_min + 0.03, LIDAR_FLOOR)   # 너무 가까운 값은 잡음인 경우가 많음
    while a <= hi + 1e-9:
        i = int(round((a - scan.angle_min) / scan.angle_increment)) % n
        r = scan.ranges[i]
        if floor < r < scan.range_max and math.isfinite(r):
            vals.append(r)
        a += step
    if not vals:
        return None
    vals.sort()
    return vals[1] if len(vals) >= 2 else vals[0]


def lidar_long_objects(scan, min_len, max_len):
    """
    라이다로 바닥 근처의 '긴 물체' 찾기 (누운 사람 후보).
    서 있는 사람은 라이다 높이에서 다리 두 개(각 10~20cm)만 보이고, 누운 사람은 몸 옆면이 길게 보임.
    연속된 점을 한 물체로 묶어서, 양 끝 길이가 min_len~max_len(m)인 것만 돌려줌.
    반환: [{"a1", "a2"(각도 rad, 로봇 기준), "r"(가운데 거리), "len", "pts"[(x, y) 로봇 기준]}]
    """
    if scan is None or scan.angle_increment == 0:
        return []
    floor = max(scan.range_min + 0.03, LIDAR_FLOOR)
    pts = []
    for i, r in enumerate(scan.ranges):
        a = scan.angle_min + i * scan.angle_increment
        a = math.atan2(math.sin(a), math.cos(a))
        if floor < r < scan.range_max and math.isfinite(r):
            pts.append((a, r, r * math.cos(a), r * math.sin(a)))
        else:
            pts.append(None)
    # 360도가 이어지도록, 빈 곳(None)에서 시작
    start = next((i for i, q in enumerate(pts) if q is None), 0)
    order = pts[start:] + pts[:start]
    groups, cur = [], []
    for q in order:
        if q is None:
            if cur:
                groups.append(cur)
            cur = []
            continue
        if cur:
            px, py = cur[-1][2], cur[-1][3]
            gap = max(LIDAR_OBJ_GAP, q[1] * abs(scan.angle_increment) * 3)
            if math.hypot(q[2] - px, q[3] - py) > gap:
                groups.append(cur)
                cur = []
        cur.append(q)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        if len(g) < 6:
            continue
        length = math.hypot(g[-1][2] - g[0][2], g[-1][3] - g[0][3])
        if not (min_len <= length <= max_len):
            continue
        mid = g[len(g) // 2]
        out.append({"a1": g[0][0], "a2": g[-1][0], "r": mid[1], "len": length,
                    "pts": [(q[2], q[3]) for q in g]})
    return out


def on_static_map(grid, mx, my):
    """지도(OccupancyGrid)에서 그 위치(주변 1칸 포함)가 벽/고정 물체면 True"""
    info = grid.info
    cx = int((mx - info.origin.position.x) / info.resolution)
    cy = int((my - info.origin.position.y) / info.resolution)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            x, y = cx + dx, cy + dy
            if 0 <= x < info.width and 0 <= y < info.height and grid.data[y * info.width + x] >= 65:
                return True
    return False


class Target:
    """여러 각도에서 본 판정을 모으는 '한 사람' (지도 위치로 같은 사람인지 구분)"""
    _next = 1

    def __init__(self, x, y, now):
        self.tid = Target._next
        Target._next += 1
        self.x, self.y = x, y
        self.votes = deque(maxlen=60)
        self.last_seen = now
        self.lying_since = None     # 누움으로 확정된 시각
        self.fell_time = None       # 넘어지는 순간을 본 시각
        self.fallen_time = None     # 쓰러짐(최종)으로 바뀐 시각
        self.ux = self.uy = None    # 마지막으로 서 있던 위치 (일어났을 때 같은 사람 찾기용)
        self.last_up = -1e9         # 마지막으로 확실히 서 있는 모습을 본 시각
        self.empty_since = None     # 그 자리를 보고 있는데 아무도 없다고 확인되기 시작한 시각
        self.seen = 1               # 본 프레임 수 (한두 번 잠깐 보인 건 '안 보임' 목록에 안 남김)
        self.still_since = None
        self.still_valid_sec = 0.0  # 상체/부분 신체 모두 누적한 유효 관찰 시간
        self.upper_still_valid_sec = 0.0  # 상체 근거가 있는 유효 관찰 시간만 누적
        self.still_last_valid_at = None
        self.still_previous_valid = False
        self.still_motion_bad_since = None  # 이전 버전과의 호환용(이제 큰 움직임으로는 차단하지 않음)
        self.last_lying_evidence = None
        self.last_upper_lying = None
        self.last_upper_source = None
        self.observed_at = None
        self.observed_pid = None
        self.up_count = 0           # 누운 뒤 '확실히 서 있음'이 연속으로 나온 횟수 (3번이면 일어난 것)

    def down(self):
        return self.lying_since is not None or self.fell_time is not None


class LyingCore:
    def __init__(self, weights, cam, min_dist=1.0, max_dist=3.0, vote_min=10, vote_ratio=0.6,
                 lidar_over_ratio=1.5, cam_offset_x=0.07, save_dir="falls", rot_every=3,
                 too_close_dist=1.0, fall_after_sec=10.0, target_keep_sec=TARGET_KEEP_SEC, partial_body_enabled=True):
        self.model = YOLO(weights)
        self.cam = cam
        self.fx = None
        self.min_dist, self.max_dist = min_dist, max_dist
        self.vote_min, self.vote_ratio = vote_min, vote_ratio
        self.lidar_over_ratio = lidar_over_ratio
        self.too_close_dist = too_close_dist
        self.partial_body_enabled = bool(partial_body_enabled)
        self.fall_after_sec = fall_after_sec
        self.partial_fall_after_sec = 6.0  # 중간 프로젝트: 부분 신체 누움 6초
        self.target_keep_sec = target_keep_sec
        self.down_keep_sec = 300.0
        self.rot = RotAssist(rot_every)
        self.cam_offset_x = cam_offset_x
        self.save_dir = os.path.expanduser(save_dir)
        os.makedirs(self.save_dir, exist_ok=True)

        self.use_track = True
        self.dark_log_t = 0.0     # 어두운 화면 로그를 마지막으로 찍은 시각
        self.history = defaultdict(lambda: deque(maxlen=SMOOTH_FRAMES))
        self.labels = {}
        self.lying_since = {}
        self.prev_kpts = {}
        self.prev_boxes = {}  # pose 관절이 적을 때, 같은 YOLO track의 박스 이동량으로 보수적 보조 측정
        self.fall = FallDetector()
        self.targets = []
        self.track_to_target = {}     # YOLO 추적 번호 -> 사람 번호 (같은 추적이 이어지면 같은 사람)
        self.last_close = None      # (시각, 로봇 방향): 마지막으로 가까운(1.5m 안) 사람을 카메라로 본 때
        self.lidar_close = 0
        self.backing = False
        self.close_run = {}
        self.back_until, self.back_reason_hold = 0.0, None
        self.pose_hist = deque()
        # 라이다로 찾는 '누운 사람 의심' (카메라가 못 잡을 때 보조)
        self.lidar_min_len, self.lidar_max_len = 1.0, 2.3
        self.static_map = None      # /map (OccupancyGrid). 있으면 벽·가구 같은 고정 물체는 제외
        self.motion_threshold = 0.18  # 진단 로그에서 움직임 큼 여부를 구분하는 기준(누적 차단 X)
        self.bbox_motion_threshold = 0.25  # 바운딩 박스 움직임 로그 기준(누적 차단 X)
        self.motion_grace_sec = 1.5  # 기존 YAML 호환용; 누움 유지 판정에는 사용하지 않음
        self.observation_gap_sec = 3.0  # 짧은 카메라 공백 동안 이전 유효시간 보존(공백 시간 자체는 누적 X)
        self.valid_sample_sec = 0.35  # 3~5 FPS에서 작은 지연을 허용하되 긴 끊김은 누적하지 않음
        self.partial_hold_sec = 1.0
        self.upper_evidence_sec = 2.0  # 다리만으로는 신규 확정 금지; 최근 상체 근거 필요
        self.low_top_height = 0.55
        self.low_horizontal_min_width = 0.80
        self.map_ok = False         # 로봇 위치가 map 좌표계일 때만 지도와 비교

    def detect(self, frame):
        kwargs = dict(conf=getattr(self, "person_conf", PERSON_CONF), imgsz=getattr(self, "inference_imgsz", 320), classes=[0], verbose=False)
        if self.use_track:
            try:
                return self.model.track(frame, persist=True, tracker="bytetrack.yaml", **kwargs)[0]
            except Exception as e:
                print("추적 기능을 쓸 수 없어 일반 모드로 실행해요:", e)
                self.use_track = False
        return self.model.predict(frame, **kwargs)[0]

    def _distance(self, box, w, scan, others=()):
        """라이다 거리와 발 위치 거리를 비교해서 하나 고르기. others = 같은 화면의 다른 사람 박스들"""
        x1, y1, x2, y2 = box
        fx = self.fx or self.cam.fy
        bearing = math.atan((self.cam.cx - (x1 + x2) / 2) / fx)
        # 박스 가로 폭의 가운데 절반 방향만 사용 (옆 물체가 섞이지 않게), 최소 ±1.5°
        a1 = math.atan((self.cam.cx - (x1 + (x2 - x1) * 0.25)) / fx)
        a2 = math.atan((self.cam.cx - (x1 + (x2 - x1) * 0.75)) / fx)
        half = max(abs(a1 - a2) / 2, math.radians(1.5))
        lidar = scan_min_range(scan, bearing - half, bearing + half)

        feet = None
        if y2 < self.cam.image_size[1] - 1 - EDGE_PX:
            feet = self.cam.dist_from_feet(y2)
            if feet is not None and feet > 8:
                feet = None

        # 화면과 맞는지 확인: 라이다 거리에 있는 바닥은 화면에서 y = cy + fy*높이/거리 에 보여야 함.
        # 박스 아래(발/몸의 가장 가까운 부분)가 그보다 한참 위에 있으면, 라이다가 사람보다 가까운
        # 다른 것(옆 사람, 로봇 몸체, 잡음)을 본 것 -> 라이다 값을 버림
        # (뒤로 가기 판단에 쓰이는 1m 안 값만 확인)
        if lidar is not None and lidar < self.too_close_dist and y2 < self.cam.image_size[1] - 1 - EDGE_PX:
            floor_y = self.cam.cy + self.cam.fy * self.cam.cam_h / max(lidar, 0.05)
            if floor_y > y2 + 50:
                lidar = None
        # 카메라가 12cm로 낮아서 발 위치 거리는 픽셀 몇 개 차이로도 크게 틀어짐 -> 라이다 우선.
        # 단, 라이다가 발 위치 거리보다 훨씬 멀면(누운 사람은 라이다 높이보다 낮아서
        # 라이다가 사람을 지나쳐 벽을 봄) 발 위치 거리를 사용.
        # 가로로 겹치고 박스 아래가 더 아래인(더 가까운) 다른 사람이 있으면, 라이다가 그 사람(앞에 선 사람의 다리,
        # 앞에 쓰러진 몸)을 봤을 수 있음: 라이다가 발 위치 거리보다 훨씬 가깝거나 발이 가려졌으면
        # 발 위치 거리를 쓰고, 그것도 없으면 거리 모름 (틀린 거리보다 모르는 게 안전)
        # (앞을 가린 사람이 없으면 라이다를 믿음: 낮은 카메라라 발 위치 거리는 잘 틀어짐)
        mid1, mid2 = x1 + (x2 - x1) * 0.25, x1 + (x2 - x1) * 0.75
        front = any(o[0] < mid2 and o[2] > mid1 and o[3] > y2 + 10 for o in others)
        if front and lidar is not None and (feet is None or lidar * self.lidar_over_ratio < feet):
            if feet is not None and feet >= self.too_close_dist:
                return feet, "feet", bearing, lidar, feet
            return None, None, bearing, lidar, feet
        if lidar is not None and feet is not None:
            # 단, 발 위치 거리가 1m 안으로 나오는 건 박스 아래가 화면 아래쪽에 걸린 경우(몸 일부 박스)라 믿지 않음
            if lidar > feet * self.lidar_over_ratio and feet >= self.too_close_dist:
                return feet, "feet", bearing, lidar, feet
            return lidar, "lidar", bearing, lidar, feet
        if lidar is not None:
            return lidar, "lidar", bearing, lidar, feet
        # 라이다 값이 없을 때도 1m 안 발 위치 거리는 믿지 않음 (몸 일부 박스. 가까운 사람은 '화면을 꽉 채움'으로 판단)
        if feet is not None and feet >= self.too_close_dist:
            return feet, "feet", bearing, lidar, feet
        return None, None, bearing, lidar, feet

    def _alive(self, now):
        # 누워 있는(쓰러진) 사람은 스스로 움직일 수 없으니 더 오래 기억 (down_keep_sec, 기본 5분)
        self.targets = [t for t in self.targets
                        if now - t.last_seen < (self.down_keep_sec if t.down() else self.target_keep_sec)]
        return self.targets

    def _find_target(self, pid, mx, my, bearing, robot, now, claimed=(), current_pids=(), upright=False, raw=None):
        """
        같은 사람 찾기 (순서대로)
          1) 같은 YOLO 추적 번호로 방금까지 본 사람 (위치가 2m 넘게 튀지 않았으면)
          2) 지도 위치가 1.2m 안인 사람
          3) 로봇에서 본 방향이 8도 안이고 2초 안에 본 사람 (거리 값이 튀었거나 거리를 모를 때)
        못 찾으면 거리를 알 때만 새 사람으로 등록
        """
        alive = self._alive(now)
        # 한 화면 안의 서로 다른 사람은 같은 번호를 가질 수 없음:
        # 이번 화면에서 이미 다른 사람이 가져간 번호, 지금 화면에 있는 다른 추적 번호의 번호는 제외
        reserved = {self.track_to_target[p] for p in current_pids
                    if p != pid and p in self.track_to_target}
        excl = set(claimed) | reserved
        alive = [t for t in alive if t not in set(claimed)]
        prev = self.track_to_target.get(pid)
        if prev is not None and prev in alive:
            # YOLO 추적 번호가 다른 사람에게 넘어가는 경우가 있음:
            # 누워 있는 사람 번호에 서 있는 모습이 오면, 그 사람이 서 있던 자리(없으면 누운 자리) 1m 안일 때만 이어 씀
            if prev.down() and raw == "not_lying" and mx is not None:
                px, py = (prev.ux, prev.uy) if prev.ux is not None else (prev.x, prev.y)
                ok = math.hypot(px - mx, py - my) < 1.0
            else:
                ok = mx is None or math.hypot(prev.x - mx, prev.y - my) < 2.0
                if ok and raw == "lying" and mx is not None:
                    # 잘못 붙어 있던 경우 바로잡기: 다른 '누운 사람'이 0.8m 넘게 더 가까우면 그쪽으로
                    dp = math.hypot(prev.x - mx, prev.y - my)
                    if any(t is not prev and t.down() and t not in excl
                           and math.hypot(t.x - mx, t.y - my) < dp - 0.8 for t in alive):
                        ok = False
            if ok:
                return self._touch(prev, mx, my, now)
        if mx is not None and upright:
            # 확실히 서 있는 모습이 '넘어졌던 사람이 서 있던 자리'(1m 안)에 보이면 그 사람이 일어난 것
            # (그 사람 번호를 누운 모습의 다른 박스가 잡고 있어도 우선)
            ups = [(math.hypot(t.ux - mx, t.uy - my), t.tid, t) for t in alive
                   if t.fell_time is not None and t.ux is not None]
            ups = [u for u in ups if u[0] < 1.0]
            if ups:
                return self._touch(min(ups)[2], mx, my, now)
        if mx is not None and raw == "lying":
            # 방금(3.5초 안) 서 있던 사람 바로 근처(1.2m)의 누운 모습: 그 사람이 넘어지는 중일 수 있음
            # -> 다른 사람 번호에 붙이지 말고 쓰러짐 감지를 기다림
            near_up = [t for t in alive if not t.down() and t.ux is not None
                       and now - t.last_up < FALL_WINDOW and math.hypot(t.x - mx, t.y - my) < TARGET_RADIUS]
            near_down = [t for t in alive if t.down() and math.hypot(t.x - mx, t.y - my) < 0.8]
            if near_up and not near_down:
                return None

            # 누운 모습인데 가장 가까운 '누운 사람'을 이미 다른 박스가 맡았으면 같은 사람의 다른 부분
            # (옆에 서 있는 사람 번호에 잘못 붙지 않게)
            downs = [(math.hypot(t.x - mx, t.y - my), t.tid, t) for t in self._alive(now) if t.down()]
            downs = [d for d in downs if d[0] < LYING_RADIUS]
            if downs:
                t = min(downs)[2]
                if t in excl or t in set(claimed):
                    return None         # 같은 사람을 다른 박스가 이미 맡음 (같은 사람의 다른 부분)
        if mx is not None:
            best, best_d = None, 99.0
            for t in alive:
                if t in excl:
                    continue
                # 서 있는 것을 확실히 본 사람에게 다른 추적 번호의 '누운 모습'은 붙이지 않음
                # (그 사람이 쓰러진 거라면 쓰러짐 감지가 번호를 이어 줌)
                if raw == "lying" and not t.down() and t.ux is not None:
                    continue
                # 서 있는 모습은 '그 사람이 마지막으로 서 있던 위치'와 비교 (누운 위치는 몸 길이만큼 옮겨져 있음)
                # 서 있는 걸 본 적 없는 누운 사람에게는 서 있는 모습을 붙이지 않음 (옆 사람과 섞이지 않게)
                if upright and t.down() and t.ux is None:
                    continue
                tx, ty = (t.ux, t.uy) if upright and t.down() else (t.x, t.y)
                d = math.hypot(tx - mx, ty - my)
                # 누운 모습끼리는 2m까지 같은 사람 (다리 쪽을 보다가 머리 쪽을 봐도 같은 번호)
                lim = LYING_RADIUS if raw != "not_lying" and t.down() else TARGET_RADIUS
                if d < lim and d < best_d:
                    best, best_d = t, d
            if best is not None:
                return self._touch(best, mx, my, now)
        rx, ry, ryaw = robot
        best, best_a = None, math.radians(8)
        for t in alive:
            # 서 있는 모습은 방향만 비슷한 '누운 사람'에 붙이지 않음 (같은 자리 1.2m 안일 때만 같은 사람 = 일어난 것)
            if t in excl or (mx is not None and now - t.last_seen > 2.0) or (raw == "not_lying" and t.down() and t is not prev) \
                    or (raw == "lying" and not t.down() and t.ux is not None):
                continue
            da = math.atan2(t.y - ry, t.x - rx) - ryaw - bearing
            a = abs(math.atan2(math.sin(da), math.cos(da)))
            if a < best_a:
                best, best_a = t, a
        if best is not None:
            return self._touch(best, mx, my, now)
        if mx is None or raw == "too_close":
            return None     # 거리를 모르거나 너무 가까워 판정이 불확실하면 새 번호를 만들지 않음
        # 새 번호를 만들기 전에: 넘어졌던 사람이 서 있던 자리(1.5m 안)에서 다시 보이면 그 사람 (일어난 것)
        best, best_d = None, 1.5
        for t in alive:
            if t.fell_time is None or t.ux is None:
                continue
            d = math.hypot(t.ux - mx, t.uy - my)
            if d < best_d:
                best, best_d = t, d
        if best is not None:
            return self._touch(best, mx, my, now)
        # 이번 화면에서 이미 잡혔거나 다른 박스가 맡은 사람과 0.6m 안이면 같은 사람을 두 번 찾은 것 (몸 일부 박스) -> 새 번호 안 만듦
        if any(math.hypot(c.x - mx, c.y - my) < (LYING_RADIUS if raw != "not_lying" and c.down() else 0.6)
               for c in excl):
            return None
        t = Target(mx, my, now)
        self.targets.append(t)
        near = min(((math.hypot(o.x - mx, o.y - my), o.tid) for o in alive), default=None)
        print(f"[{time.strftime('%H:%M:%S')}] 새 사람 #{t.tid} 위치 ({mx:.2f}, {my:.2f})"
              + (f", 가장 가까운 기존 #{near[1]}까지 {near[0]:.2f}m" if near else ""))
        return t

    @staticmethod
    def _touch(t, mx, my, now):
        if mx is not None:
            # 누워 있는 사람은 움직이지 않으므로 위치를 거의 고정 (처음 몇 번은 빨리 맞춰 감: 첫 값이 틀릴 수 있음)
            a = (0.05 if t.seen > 10 else 0.3) if t.down() else 0.2
            t.x = (1 - a) * t.x + a * mx
            t.y = (1 - a) * t.y + a * my
        t.last_seen = now
        t.seen += 1
        return t

    def _match_target(self, mx, my, now):
        self._alive(now)
        best, best_d = None, TARGET_RADIUS
        for t in self.targets:
            d = math.hypot(t.x - mx, t.y - my)
            if d < best_d:
                best, best_d = t, d
        if best is None:
            best = Target(mx, my, now)
            self.targets.append(best)
        else:
            best.x = 0.8 * best.x + 0.2 * mx
            best.y = 0.8 * best.y + 0.2 * my
        best.last_seen = now
        return best

    def _motion(self, pid, kxy, kconf, box, now):
        """관절 3개 공통이면 pose 기반, 없으면 동일 추적 박스의 움직임 보조 측정.

        ROI가 다른 대상으로 갑자기 이동한 경우(IoU 불량)는 움직임을 '모름'으로
        취급한다. 박스 보조는 같은 대상의 최근 상체 누움 근거가 있을 때만
        최종 관찰에 사용되도록 호출부에서 별도로 제한한다.
        """
        good = kconf >= KPT_TH
        prev = self.prev_kpts.get(pid)
        self.prev_kpts[pid] = (kxy.copy(), good, now)
        previous_box = self.prev_boxes.get(pid)
        self.prev_boxes[pid] = (tuple(float(x) for x in box), now)
        if prev is not None:
            pk, pg, previous_time = prev
            dt = now - previous_time
            if 0 < dt <= min(self.observation_gap_sec, 0.6):
                both = good & pg
                if both.sum() >= 3:
                    disp = np.linalg.norm(kxy[both] - pk[both], axis=1)
                    return float(np.median(disp) / max(box[3] - box[1], 1.0) / dt), "pose"
        if previous_box is None:
            return None, None
        pb, prev_at = previous_box
        dt = now - prev_at
        if not 0 < dt <= min(self.observation_gap_sec, 0.6):
            return None, None
        x1, y1, x2, y2 = [float(x) for x in box]
        px1, py1, px2, py2 = pb
        inter = max(0, min(x2, px2)-max(x1, px1)) * max(0, min(y2, py2)-max(y1, py1))
        union = max((x2-x1)*(y2-y1) + (px2-px1)*(py2-py1) - inter, 1.0)
        if inter/union < 0.65:  # 다른 대상/신체 일부만 새로 검출된 프레임 보호
            return None, None
        bh = max(y2-y1, py2-py1, 1.0)
        dc = math.hypot((x1+x2-px1-px2)*0.5, (y1+y2-py1-py2)*0.5)
        ds = max(abs((x2-x1)-(px2-px1)), abs((y2-y1)-(py2-py1)))
        return float(max(dc, ds*0.5)/bh/dt), "bbox"

    @staticmethod
    def _clear_still(target):
        target.still_since = None
        target.still_valid_sec = 0.0
        target.upper_still_valid_sec = 0.0
        target.still_last_valid_at = None
        target.still_previous_valid = False
        target.still_motion_bad_since = None

    def reset_observation(self):
        self.prev_kpts.clear()
        self.prev_boxes.clear()
        self.history.clear()
        self.labels.clear()
        self.fall = FallDetector()
        for t in self.targets:
            self._clear_still(t)
            t.last_lying_evidence = None
            t.last_upper_lying = None
            t.last_upper_source = None
            t.observed_at = None
            t.observed_pid = None
            t.fallen_time = None  # 카메라 수신/추론 오류 후에는 유효 누움 시간을 다시 확보해야 함
            t.fell_time = None

    def observe_still(self, target, pid, now, raw, motion, moving,
                      motion_source="pose", eligible=True, strong_upright=False,
                      upper_eligible=True):
        """같은 사람의 관찰된 누움 상태가 유지된 유효시간을 누적.

        - 상체 관절/형태 근거 있음: 누움 관찰 5초(fall_after_sec)면 의심 확정.
        - 일부 신체만 보임: 누움 관찰 6초면 의심 확정.
        - 통증으로 팔다리를 움직여도 raw=lying 및 누움 형태 검증을 유지하면 누적.
          motion_too_large는 진단용이며 누적 차단·초기화 조건이 아님.
        - 판단 불가/움직임 수치 미확인/영상 누락 동안은 추가 누적하지 않음.
        - 짧은 영상 누락(최대 3초)은 누적값 보존(누락 시간 미포함).
        - 확실한 기립/로봇 이동/3초 초과 관찰 공백은 기존대로 초기화.
        """
        valid = (raw == "lying" and eligible and motion is not None and
                 math.isfinite(motion) and not moving)
        clear = (strong_upright or raw == "not_lying" or moving)
        last = target.still_last_valid_at
        interrupted = (target.observed_at is not None and
                       (now <= target.observed_at or
                        now - target.observed_at > self.observation_gap_sec))
        stale = last is not None and (now < last or now-last > self.observation_gap_sec)
        if clear or interrupted or stale:
            self._clear_still(target)
        # 최근 상체 근거가 사라지면 5초용 상체 타이머만 초기화하고,
        # 부분 신체 6초용 전체 타이머는 유지한다.
        if not upper_eligible:
            target.upper_still_valid_sec = 0.0
        if valid:
            if target.still_since is None:
                target.still_since = now
            elif target.observed_at is not None:
                dt = now - target.observed_at
                # A recovered frame must not turn a camera dropout into observed time.
                # Count only reasonably continuous samples (5 FPS ~0.2s, 3 FPS ~0.33s).
                if 0 < dt <= min(self.observation_gap_sec, max(0.6, self.valid_sample_sec * 1.5)):
                    delta = min(dt, self.valid_sample_sec)
                    target.still_valid_sec += delta
                    if upper_eligible:
                        target.upper_still_valid_sec += delta
            target.still_last_valid_at = now
            target.still_previous_valid = True
        else:
            target.still_previous_valid = False
        target.observed_at, target.observed_pid = now, pid
        confirmed_upper = (valid and upper_eligible and
                           target.upper_still_valid_sec >= self.fall_after_sec)
        confirmed_partial = (valid and target.still_valid_sec >=
                             getattr(self, "partial_fall_after_sec", 6.0))
        return target.still_valid_sec, (confirmed_upper or confirmed_partial)

    def process(self, frame, scan=None, robot_pose=None, now=None):
        """한 프레임 처리. (결과 dict, 확인용 화면) 반환"""
        now = time.time() if now is None else now
        h, w = frame.shape[:2]
        self.cam.setup(w, h)
        self.fx = self.cam.fx
        clean = frame.copy()
        res = self.detect(frame)
        # 화면이 너무 어두우면(카메라 켜지는 순간, 렌즈 가림, 불 꺼진 방) 이번 프레임 사람 검출은 버림.
        # 라이다 뒤로 가기는 아래에서 그대로 동작
        bright = float(cv2.cvtColor(cv2.resize(clean, (80, 60)), cv2.COLOR_BGR2GRAY).mean())
        dark = bright < DARK_MEAN
        if dark and now - self.dark_log_t > 5.0:
            print("화면이 너무 어두워서 판정을 건너뛰어요")
            self.dark_log_t = now
        rx, ry, ryaw = robot_pose if robot_pose else (0.0, 0.0, 0.0)
        people = []
        fall_image = None
        fall_reason = None

        hy = int(self.cam.horizon_y())
        if 0 <= hy < h:
            cv2.line(frame, (0, hy), (w, hy), (0, 255, 255), 1)

        # 라이다 긴 물체 (누운 사람 후보): 지도 좌표로 바꾸고, 지도에 있는 벽·가구는 뺌
        objs = []
        cyaw, syaw = math.cos(ryaw), math.sin(ryaw)
        for o in lidar_long_objects(scan, self.lidar_min_len, self.lidar_max_len):
            mpts = [(rx + cyaw * x - syaw * y, ry + syaw * x + cyaw * y) for x, y in o["pts"]]
            if self.static_map is not None and self.map_ok:
                if sum(on_static_map(self.static_map, x, y) for x, y in mpts) >= len(mpts) * 0.5:
                    continue
            o["mx"] = sum(x for x, _ in mpts) / len(mpts)
            o["my"] = sum(y for _, y in mpts) / len(mpts)
            o["camera"] = None
            objs.append(o)

        # 로봇 움직임 기록: 최근(쓰러짐 비교 시간 + 1초) 안에 돌거나 움직였는지
        self.pose_hist.append((now, rx, ry, ryaw))
        while self.pose_hist and now - self.pose_hist[0][0] > FALL_WINDOW + 1.0:
            self.pose_hist.popleft()
        moving = any(abs(math.atan2(math.sin(q[3] - ryaw), math.cos(q[3] - ryaw))) > 0.06
                     or math.hypot(q[1] - rx, q[2] - ry) > 0.1 for q in self.pose_hist)

        dets = [] if dark else unpack(res)
        if not dark:
            dets += self.rot.extra(self.model, clean, dets)
        dets = dedupe(dets)
        dets = drop_rot_on_upright(dets)
        # 조금 어두운 화면에서 화면 대부분을 덮는데 관절이 거의 없는 박스는 사람이 아님 (어두운 화면 오탐)
        if bright < DIM_MEAN:
            dets = [d for d in dets if not ((d[0][2] - d[0][0]) * (d[0][3] - d[0][1]) > w * h * BIG_BOX_RATIO
                                            and int((d[3] >= KPT_TH).sum()) <= 2)]
        # 회전 검출로 찾은 '누운 사람'은 바닥 위에 있어야 함: 박스 아래가 카메라 높이 선보다 아래
        dets = [d for d in dets if not (d[1] >= 10000 and d[0][3] < self.cam.horizon_y())]
        current_pids = {d[1] for d in dets}
        claimed = set()
        # 이번 프레임에서 확실히 서 있는 사람 (화면 가로 범위). 같은 자리에 아직 서 있는 사람이 있으면
        # 그 자리의 누움은 그 사람이 쓰러진 게 아님 (뒤에 있던 다른 누운 사람)
        upright_now = []
        for box, pid, kxy, kconf in dets:
            d_ = self._distance(box, w, scan, [o[0] for o in dets if o[0] is not box])
            r_, _, m_ = judge(box, kxy, kconf, w, h, self.cam, d_[0], self.labels.get(pid),
                              dist_src=d_[1] or "", partial_body_enabled=self.partial_body_enabled)
            h_ = next((v for v in (m_["head_h"], m_["kp_top_h"], m_["top_h"]) if v is not None), None)
            if (r_ == "not_lying" and d_[0] is not None and int((kconf >= KPT_TH).sum()) >= 4 and h_ is not None
                    and (h_ >= UPRIGHT_STRONG_H or (m_["top_cut"] and m_["top_h"] >= UPRIGHT_MIN_H))):
                upright_now.append((pid, float(box[0] / w), float(box[2] / w), d_[0]))
        if dets:
            for box, pid, kxy, kconf in dets:
                rotated = pid >= 10000
                # 회전 검출로 찾은 '누운 사람'은 바닥 위에 있어야 함: 박스 아래가 카메라 높이 선보다 아래
                if rotated and box[3] < self.cam.horizon_y():
                    continue
                dist, src, bearing, lidar_d, feet_d = self._distance(box, w, scan,
                                                                     [o[0] for o in dets if o[0] is not box])
                # 라이다가 사람을 지나쳐 멀리(벽)를 본 경우: 누운 사람은 라이다 높이(약 17cm)보다 낮을 수 있음.
                # 관절이 3개 이상 보이는 확실한 사람인데 발 위치로는 1m 안이고 라이다는 1.5배 넘게 멀면
                # 라이다를 믿지 않고 발 위치 거리를 씀 (먼 거리로 계산하면 누운 사람이 서 있는 것처럼 높게 나옴)
                if (src == "lidar" and feet_d is not None and feet_d < self.too_close_dist
                        and lidar_d > feet_d * self.lidar_over_ratio and int((kconf >= KPT_TH).sum()) >= 3
                        and box[3] < h - 1 - EDGE_PX):
                    dist, src = feet_d, "feet"
                raw, why, m = judge(box, kxy, kconf, w, h, self.cam, dist,
                                    self.labels.get(pid), dist_src=src or "",
                                    partial_body_enabled=self.partial_body_enabled)
                # 관절이 거의 안 보이는 박스(빈 바닥까지 크게 잡힌 박스 등)는 모양으로 판단하지 않음
                n_kpt = int((kconf >= KPT_TH).sum())
                # '안 누움'은 관절 4개 이상, '누움'은 3개 이상(다리 하나: 엉덩이·무릎·발목)일 때만 인정
                if (raw == "not_lying" and n_kpt < 4) or (raw == "lying" and n_kpt < 3):
                    # 머리가 낮고 몸이 수평으로 길게 보이는 강한 영상/거리 근거가 있으면
                    # 상체 관절이 1~2개여도 한 프레임의 누움 후보를 허용한다.
                    # 다리만 보이거나 거리 정보가 불신이면 여전히 판단 불가.
                    if not (raw == "lying" and n_kpt >= 1 and
                            strong_low_horizontal_upper(
                                box, kxy, kconf, w, h, m,
                                self.low_top_height, self.low_horizontal_min_width)):
                        raw, why = "unknown", f"관절이 {n_kpt}개만 보임, 판단 안 함"
                    else:
                        why = f"상체 수평·낮은 머리 + 관절 {n_kpt}개 (추가 관찰 필요)"
                # 라이다 보조: 카메라가 사람은 찾았는데 판단을 못 했을 때(unknown)만 사용.
                # 같은 방향·거리에 바닥 근처 긴 물체(1.0~2.3m)가 있으면 누운 사람의 일부로 보고 누움
                lid_obj = None
                if dist is not None:
                    for o in objs:
                        lo, hi = min(o["a1"], o["a2"]), max(o["a1"], o["a2"])
                        if lo - 0.05 <= bearing <= hi + 0.05 and abs(o["r"] - dist) < 0.6:
                            lid_obj = o
                            break
                if lid_obj is not None:
                    lid_obj["camera"] = raw
                    if raw == "unknown":
                        raw, why = "lying", f"라이다: 바닥 근처 {lid_obj['len']:.1f}m 긴 물체 + 사람 일부"

                # 정말 몸이 잘려 관절이 거의 안 보이는 '극근거리'에서만 TOO CLOSE.
                # 0.6~0.7m여도 관절/높이로 판단 가능하면 원래 lying/unknown 판정을 유지한다.
                if genuinely_too_close(box, m, n_kpt, dist, self.too_close_dist, h,
                                       lidar_present=lidar_d is not None) and raw != "lying":
                    raw = "too_close"
                    why = (f"{dist:.2f}m, 신체 잘림 + 관절 {n_kpt}개" if dist is not None
                           else f"화면을 가득 채움 + 관절 {n_kpt}개")
                # 관절이 하나도 없어서 판단 불가인 박스는 표에 올리지 않음 (잡혔다 놓쳤다 하면서
                # 누운 사람이 '판단 불가' / '안 보임'으로 번갈아 바뀌는 것 방지). 안 보임(hidden)으로 계속 기억됨
                if raw == "unknown" and n_kpt == 0:
                    continue
                cx_ratio = float((box[0] + box[2]) / 2 / w)

                # 서 있음의 확실함: 머리 높이 > 찾은 관절 중 가장 높은 곳 > 박스 꼭대기 순으로 사용
                hgt = next((v for v in (m["head_h"], m["kp_top_h"], m["top_h"]) if v is not None), None)
                if n_kpt < 4:
                    hgt = None                  # 관절이 거의 없는 박스는 '서 있음' 근거로도 안 씀
                # 가까워서 위가 잘렸는데 화면 위 끝이 0.6m 이상: 몸이 그 위로 더 이어짐 = 서 있음
                # (카메라가 낮아서 약 1.6m 안에서는 서 있는 사람도 머리가 안 보여 키 0.8m를 확인할 수 없음)
                tall_cut = (hgt is not None and m["top_cut"] and m["top_h"] is not None
                            and m["top_h"] >= UPRIGHT_MIN_H)
                # 확실히 서 있음: 거리를 알고 키가 0.8m 넘을 때만 (거리 모르는 다리 조각은 서 있음 근거로 안 씀)
                strong = dist is not None and hgt is not None and (hgt >= UPRIGHT_STRONG_H or tall_cut)
                is_fall = self.fall.update(pid, raw, cx_ratio, now, strong, robot=(rx, ry, ryaw), moving=moving,
                                           span=(float(box[0] / w), float(box[2] / w)), upright_now=upright_now,
                                           dist=dist)
                if is_fall and fall_image is None:
                    fall_image = os.path.join(self.save_dir, f"fall_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
                    cv2.imwrite(fall_image, clean)
                    print(f"[{time.strftime('%H:%M:%S')}] #{pid} 쓰러짐 감지! ({why}) -> {fall_image}")

                hist = self.history[pid]
                hist.append(raw)
                votes = Counter(x for x in hist if x in VOTE_LABELS)
                if raw == "too_close":
                    label = "too_close"
                elif not votes:
                    label = raw
                else:
                    label = votes.most_common(1)[0][0]
                    if label == "lying" and votes["lying"] < LYING_MIN_FRAMES:
                        label = "unknown"       # 누움이 아직 몇 번 안 나옴 -> 확정 보류
                if label != self.labels.get(pid):
                    print(f"[{time.strftime('%H:%M:%S')}] #{pid} {LABEL_KO[label]} ({why})")
                self.labels[pid] = label

                if label == "lying":
                    self.lying_since.setdefault(pid, now)
                else:
                    self.lying_since.pop(pid, None)
                lying_sec = now - self.lying_since[pid] if pid in self.lying_since else 0.0

                # 사람의 지도 좌표 + 여러 각도 투표
                map_x = map_y = None
                tgt = None
                in_range = dist is not None and self.min_dist <= dist <= self.max_dist
                if dist is not None:
                    d_base = dist + self.cam_offset_x
                    map_x = rx + d_base * math.cos(ryaw + bearing)
                    map_y = ry + d_base * math.sin(ryaw + bearing)
                # 확실히 서 있음: 키가 0.8m 이상, 또는 가까워서 위가 잘렸는데 관절이 0.55m 넘게 올라와 있거나
                # 화면 위 끝이 0.6m 이상 (누운 사람의 관절은 0.5m를 넘지 않음)
                upright = raw == "not_lying" and hgt is not None and (
                    hgt >= UPRIGHT_STRONG_H or tall_cut
                    or (m["top_cut"] and m["kp_top_h"] is not None and m["kp_top_h"] >= 0.55))
                if raw == "too_close":
                    # 너무 가까우면 거리·모양이 부정확: 같은 추적 번호로 이미 붙어 있던 사람만 이어 쓰고 위치는 안 바꿈
                    tgt = self.track_to_target.get(pid)
                    if tgt is not None and (tgt not in self._alive(now) or tgt in claimed):
                        tgt = None
                    # 추적 번호가 다른 사람에게 넘어간 경우 막기: 위치가 너무 다르면 연결 안 함
                    if tgt is not None and map_x is not None and \
                            math.hypot(tgt.x - map_x, tgt.y - map_y) > (LYING_RADIUS if tgt.down() else TARGET_RADIUS):
                        tgt = None
                    if tgt is not None:
                        tgt.last_seen = now
                else:
                    tgt = self._find_target(pid, map_x, map_y, bearing, (rx, ry, ryaw), now,
                                            claimed=claimed, current_pids=current_pids, upright=upright, raw=raw)
                if is_fall:
                    # 쓰러짐: 직전에 그 자리에 서 있던 사람의 번호를 그대로 이어받음 (옆에 누운 다른 사람 번호로 안 가게)
                    before = self.track_to_target.get(self.fall.src_pid)
                    near = (before is not None and (map_x is None or before.ux is None
                            or math.hypot(before.ux - map_x, before.uy - map_y) < 2.0))
                    if near and before in self._alive(now) and before not in claimed:
                        tgt = before
                if tgt is not None:
                    self.track_to_target[pid] = tgt
                    claimed.add(tgt)
                    if upright and map_x is not None:
                        tgt.ux, tgt.uy = map_x, map_y
                        tgt.last_up = now
                # 관절 2개/일시적 검출 누락: 기존 누움 증거를 의심으로만 보존.
                # raw=unknown 자체는 유효 판정 시간에 더하지 않는다.
                partial_suspected = retain_partial_suspicion(
                    tgt, raw, n_kpt, now, self.partial_hold_sec, moving=moving)
                if tgt is not None and raw == "lying":
                    tgt.last_lying_evidence = now
                    has_upper, evidence_source = evidence_for_upper_lying(
                        box, kxy, kconf, w, h, m,
                        top_limit=self.low_top_height,
                        min_width=self.low_horizontal_min_width)
                    if has_upper:
                        tgt.last_upper_lying = now
                        tgt.last_upper_source = evidence_source
                elif tgt is not None and upright:
                    tgt.last_lying_evidence = None
                    tgt.last_upper_lying = None
                    tgt.last_upper_source = None

                if tgt is not None and dist is not None:
                    if is_fall:
                        tgt.votes.clear()       # 쓰러지기 전 '안 누움' 투표는 버림
                    if in_range and raw in VOTE_LABELS:
                        tgt.votes.append(raw)
                if tgt is not None:
                    n_ly = sum(1 for v in tgt.votes if v == "lying")
                    n_all = len(tgt.votes)
                else:
                    n_ly = n_all = 0
                if n_all >= self.vote_min:
                    decision = "lying" if n_ly / n_all >= self.vote_ratio else "not_lying"
                else:
                    decision = "pending"

                # 상태값: normal(정상) -> lying(누워 있음) 또는 fell(넘어짐) -> fallen(쓰러짐)
                #   lying : 누움으로 확정 (넘어지는 순간은 못 봄)
                #   fell  : 서 있다가 갑자기 누운 순간을 봄
                #   fallen: lying 또는 fell 상태가 fall_after_sec 넘게 이어짐 (최종 경고)
                #   일어나서 투표가 '누움 아님'이 되면 normal로 돌아감
                if is_fall:
                    fall_reason = "fell"
                motion, motion_source = self._motion(pid, kxy, kconf, box, now)
                # 상체 관절이 없더라도 낮고 가로로 긴 일부 신체가 누워 있고
                # 충분한 관절과 신뢰할 수 있는 거리 근거가 있으면 6초 관찰 후보.
                # 첫/두 번째 사진처럼 무릎·발목만 검출되는 경우도 여기에 해당.
                upper_recent = (tgt is not None and tgt.last_upper_lying is not None and
                                0 <= now-tgt.last_upper_lying <= self.upper_evidence_sec)
                partial_candidate = (tgt is not None and eligible_partial_lying(
                    box, m, n_kpt, dist, src, raw,
                    low_top_height=self.low_top_height,
                    enabled=self.partial_body_enabled))
                # 중간 프로젝트용 판정 완화: 같은 사람에게서 lying이 반복 확인되면
                # 추가 상체·신체 모양 조건이 부족해도 부분신체 6초 관찰을 허용한다.
                # 단, 원본 영상이 실제로 raw=lying이어야 하며 (unknown 제외),
                # 충분한 누움 투표(3회)와 안정된 누움 라벨이 필요하다.
                # 로봇 이동은 차단하지만, 누운 상태의 팔다리 움직임은 허용한다.
                # 사람이 일어났거나 원본 판정이 불확실한 프레임은 기존처럼 누적 X.
                relaxed_candidate = bool(tgt is not None and raw == "lying" and
                                         label == "lying" and n_ly >= 3)
                partial_observable = partial_candidate or relaxed_candidate
                eligible = bool((upper_recent or partial_observable) and not moving)
                observation_mode = ("upper_5s" if upper_recent else
                                    f"partial_{self.partial_fall_after_sec:g}s" if partial_observable else "none")
                if moving:
                    block_reason = "robot_moving"
                elif raw != "lying":
                    block_reason = "raw_not_lying"
                elif not (upper_recent or partial_observable):
                    block_reason = "lying_shape_unconfirmed"
                elif motion is None or not math.isfinite(motion):
                    block_reason = "motion_unavailable"
                else:
                    block_reason = ""
                # 진단 목적만: 큰 움직임이 있어도 누움 상태를 계속 관찰했다면 유효.
                motion_high_allowed = bool(
                    not block_reason and motion is not None and math.isfinite(motion) and
                    motion > (self.bbox_motion_threshold if motion_source == "bbox"
                              else self.motion_threshold))
                still_sec = 0.0
                state, state_sec = ("lying" if label == "lying" else "normal"), 0.0
                if tgt is not None:
                    if is_fall and tgt.fell_time is None:
                        tgt.fell_time = now
                    if (decision == "lying" or label == "lying") and tgt.lying_since is None:
                        tgt.lying_since = now           # 화면 판정이 누움으로 안정되면 시간 재기 시작
                    elif tgt.down() and not is_fall:
                        # 일어남 판단: 머리/꼭대기가 0.8m 넘는 모습이 3번 연속, 또는 투표가 '누움 아님'
                        # (몸 일부만 보이거나 거리를 모를 때 한 번 튄 값으로는 정상으로 안 돌아감)
                        tgt.up_count = tgt.up_count + 1 if upright else 0
                        # (투표만으로는 정상으로 안 돌림: 다리만 보이는 각도에서 '안 누움' 표가 섞일 수 있음)
                        if tgt.up_count >= 3:
                            tgt.votes.clear()   # 예전 '누움' 투표 버림 (일어난 뒤 새로 모음)
                            tgt.lying_since = tgt.fell_time = tgt.fallen_time = None
                            tgt.last_lying_evidence = tgt.last_upper_lying = None
                            tgt.last_upper_source = None
                            self._clear_still(tgt)
                            tgt.up_count = 0
                    starts = [t for t in (tgt.lying_since, tgt.fell_time) if t is not None]
                    start = min(starts) if starts else None
                    still_sec, confirmed = self.observe_still(
                        tgt, pid, now, raw, motion, moving,
                        motion_source=motion_source, eligible=eligible,
                        strong_upright=upright, upper_eligible=bool(upper_recent))
                    if confirmed or tgt.fallen_time is not None:
                        if tgt.fallen_time is None:
                            tgt.fallen_time = now
                            if fall_image is None:
                                fall_image = os.path.join(
                                    self.save_dir, f"fall_{time.strftime('%Y%m%d_%H%M%S')}_fallen.jpg")
                                cv2.imwrite(fall_image, clean)
                                fall_reason = "fallen"
                            confirm_kind = ("상체" if tgt.upper_still_valid_sec >= self.fall_after_sec
                                            else "부분 신체")
                            confirm_sec = (self.fall_after_sec if confirm_kind == "상체"
                                           else self.partial_fall_after_sec)
                            print(f"[{time.strftime('%H:%M:%S')}] #{pid} 낙상 의심! "
                                  f"{confirm_kind} {confirm_sec:.0f}초 유효 누움 관찰 -> {fall_image}")
                        state = "fallen"
                    elif tgt.fell_time is not None:
                        state = "fell"
                    elif tgt.lying_since is not None:
                        state = "lying"
                    elif label != "lying":
                        state = "normal"
                    state_sec = now - start if start is not None else 0.0
                elif is_fall:
                    state = "fell"

                # 화면과 /pose_state에 쓰는 사람 번호: 지도 위치로 묶은 번호 (YOLO 추적 번호는 자주 바뀜)
                show_id = tgt.tid if tgt is not None else 1000 + int(pid) % 1000
                if rotated and label not in ("lying", "too_close") and state == "normal" and not is_fall:
                    continue                    # 회전 검출은 누움이 확인된 것만 보여 줌 (벽·바닥 오탐 방지). 너무 가까움은 뒤로 가기 신호라 남김
                # 잠깐 나타났다 사라지는 박스는 표시 안 함: 번호가 없거나 3프레임 미만으로 본 '정상' 박스
                # (너무 가까움 박스는 뒤로 가기 신호라 그대로 표시)
                if raw != "too_close" and state == "normal" and not is_fall and (tgt is None or tgt.seen < 3):
                    continue

                def r2(v):
                    return None if v is None else round(float(v), 3)
                people.append({
                    "id": show_id, "track_id": int(pid), "target_id": tgt.tid if tgt else None,
                    "label": label, "raw": raw, "why": why, "decision": decision,
                    "votes_lying": n_ly, "votes_total": n_all,
                    "dist": r2(dist), "dist_src": src, "lidar_dist": r2(lidar_d), "feet_dist": r2(feet_d),
                    "in_range": bool(in_range), "too_close": raw == "too_close",
                    "bearing_deg": round(math.degrees(bearing), 1), "cx_ratio": round(cx_ratio, 3),
                    "head_h": r2(m["head_h"]), "top_h": r2(m["top_h"]), "width": r2(m["width"]),
                    "top_cut": bool(m["top_cut"]), "side_cut": bool(m["side_cut"]),
                    "map_x": r2(map_x), "map_y": r2(map_y),
                    "lying_sec": round(lying_sec, 1), "motion": r2(motion),
                    "motion_unit": "bbox_height_per_second", "motion_source": motion_source,
                    "still_sec": round(still_sec, 2),
                    "upper_still_sec": round(tgt.upper_still_valid_sec, 2) if tgt else 0.0,
                    "observation_mode": observation_mode,
                    "partial_fall_after_sec": self.partial_fall_after_sec,
                    "fall_evidence": ("upper" if tgt and tgt.fallen_time is not None
                                      and tgt.upper_still_valid_sec >= self.fall_after_sec
                                      else "partial" if tgt and tgt.fallen_time is not None else None),
                    "partial_suspected": bool(partial_suspected),
                    "upper_evidence_recent": bool(upper_recent),
                    "upper_evidence_source": tgt.last_upper_source if tgt else None,
                    "valid_keypoint_indices": [i for i in range(len(kconf))
                                               if ok_kpt(kxy, kconf, i, w, h)],
                    "valid_block_reason": block_reason,
                    "high_motion_allowed": motion_high_allowed,
                    "observation_valid": (raw == "lying" and eligible and motion is not None
                                          and math.isfinite(motion) and not moving),
                    "fall": bool(self.fall.showing(pid, now)),
                    "state": state, "state_sec": round(state_sec, 1), "fallen": state == "fallen",
                    "box": [int(v) for v in box],
                })
                fall_on = state in ("fell", "fallen") or self.fall.showing(pid, now)
                draw_person(frame, box, kxy, kconf, label, show_id, lying_sec, m, fall_on, True,
                            tag=STATE_EN.get(state) if state != "normal" else None)

        # 화면에서 잠깐 사라진 사람은 관찰 시간을 일시 보존 (보지 못한 시간은 누적 금지)
        hidden = []
        fx_h = self.fx or self.cam.fy
        half_fov_h = math.atan((w / 2) / fx_h)
        for t in list(self._alive(now)):
            if t in claimed or not t.down() or t.seen < 5:
                continue
            # 그 자리를 똑바로 보고 있는데(1~3m, 화면 안쪽) 카메라도 못 찾고 라이다도 그 뒤까지 비어 있으면
            # 3초 뒤 '그 자리에 아무도 없음'으로 보고 잊음 (사람이 일어나서 간 경우). 못 보는 동안은 계속 기억
            ang = math.atan2(t.y - ry, t.x - rx) - ryaw
            ang = math.atan2(math.sin(ang), math.cos(ang))
            d_t = math.hypot(t.x - rx, t.y - ry) - self.cam_offset_x
            free = None
            if abs(ang) < half_fov_h * 0.8 and self.min_dist <= d_t <= self.max_dist and scan is not None:
                r_l = scan_min_range(scan, ang - math.radians(3), ang + math.radians(3))
                free = r_l is None or r_l > d_t + 0.6
            if free:
                t.empty_since = t.empty_since if t.empty_since is not None else now
                if now - t.empty_since >= 3.0:
                    print(f"[{time.strftime('%H:%M:%S')}] #{t.tid} 자리를 봤는데 아무도 없어서 잊음")
                    self.targets.remove(t)
                    continue
            else:
                t.empty_since = None
            starts = [x for x in (t.lying_since, t.fell_time) if x is not None]
            start = min(starts)
            # 화면 밖에서는 유효 누움 시간을 쌓지 않으며 3초 넘게 다시 안 보이면 초기화.
            # 잠깐 놓쳤을 때는 이전 기록을 유지하되 시간에 더하지 않는다.
            t.still_previous_valid = False
            if t.still_last_valid_at is None or now-t.still_last_valid_at > self.observation_gap_sec:
                self._clear_still(t)
            t.observed_at = now
            t.observed_pid = None
            st = "fallen" if t.fallen_time is not None else "unknown"
            hidden.append({"id": t.tid, "target_id": t.tid, "state": st,
                           "state_sec": round(now - start, 1), "fallen": st == "fallen",
                           "map_x": round(t.x, 3), "map_y": round(t.y, 3),
                           "unseen_sec": round(now - t.last_seen, 1),
                           "observation_valid": False, "still_sec": 0.0,
                           "votes_lying": sum(1 for v in t.votes if v == "lying"),
                           "votes_total": len(t.votes)})

        # 순찰 매니저가 볼 사람: 쓰러짐 > 누움 결정 > 누움 판정 > 가장 가까운 사람
        target = None
        if people:
            def prio(i):
                p = people[i]
                return (-STATE_ORDER.get(p["state"], 0), p["decision"] != "lying", p["label"] != "lying",
                        p["dist"] if p["dist"] is not None else 99)
            target = min(range(len(people)), key=prio)

        cv2.rectangle(frame, (0, 0), (w, 36), (30, 30, 30), -1)
        cv2.putText(frame, f"people: {len(people)}", (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2)
        if any(p["state"] in ("fell", "fallen") for p in people):
            cv2.putText(frame, "FALL DETECTED", (w // 2 - 120, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (0, 0, 255), 3)

        # 뒤로 가기: 사람이 1m 안 / 또는 방금(3초 안) 가까운(1.5m 안) 사람을 카메라로 봤고 그 뒤로 로봇이
        # 거의 안 돌았는데 정면 라이다가 1m 안 (가까이 가면 몸 일부만 보여서 YOLO가 사람을 놓치는 경우 대비).
        # 벽·장애물만 있을 때나, 멀리 있던 사람을 본 뒤 돌아서 장애물을 만난 경우는 뒤로 가기 안 함
        if any(p["too_close"] or (p["dist"] is not None and p["dist"] < self.too_close_dist + 0.5) for p in people):
            self.last_close = (now, ryaw)
        fx = self.fx or self.cam.fy
        half_fov = math.atan((w / 2) / fx)
        front_min = scan_min_range(scan, -half_fov, half_fov)
        # 라이다는 보조로만 씀 (따로 결과를 내지 않음):
        # 이미 누워 있다고 아는 사람 자리에 라이다로 긴 물체가 계속 보이면, 카메라에 안 보여도 그 사람을 계속 기억
        for o in objs:
            for t in self._alive(now):
                if t.down() and math.hypot(t.x - o["mx"], t.y - o["my"]) < LYING_RADIUS:
                    t.last_seen = max(t.last_seen, now - 1.0)

        # 사람이 1m 안: 같은 사람이 2프레임 연속 '너무 가까움'일 때만 (한 프레임 튄 값 무시)
        close_ids = {p["track_id"] for p in people if p["too_close"]}
        self.close_run = {k: self.close_run.get(k, 0) + 1 for k in close_ids}
        back_up = any(v >= 2 for v in self.close_run.values())
        back_reason = "person" if back_up else None
        if not back_up and front_min is not None and front_min < self.too_close_dist:
            # 사람을 방금 봤거나, 이미 뒤로 가는 중이면(정면이 계속 가까우면) 유지
            # 라이다 값 하나가 튀는 걸 막으려고 2프레임 연속일 때만
            self.lidar_close += 1
            lc = self.last_close
            saw_close = (lc is not None and now - lc[0] < 3.0
                         and abs(math.atan2(math.sin(ryaw - lc[1]), math.cos(ryaw - lc[1]))) < math.radians(20))
            if self.lidar_close >= 2 and (saw_close or self.backing):
                back_up, back_reason = True, "lidar"
        elif front_min is not None:         # 라이다 값이 빈 프레임은 연속 횟수를 그대로 둠
            self.lidar_close = 0
        # 라이다 값이 한 프레임 비는 건(front_min None) '모름'이라 뒤로 가는 중 상태를 그대로 둠
        if front_min is not None and front_min >= self.too_close_dist + 0.1:
            self.backing = False
        elif back_up:
            self.backing = True
        # 켜졌다 꺼졌다 하지 않게: 한 번 켜지면 1초는 유지
        if back_up:
            self.back_until, self.back_reason_hold = now + 1.0, back_reason
        elif now < self.back_until:
            back_up, back_reason = True, self.back_reason_hold
        if back_up:
            cv2.putText(frame, "TOO CLOSE - BACK UP", (w // 2 - 140, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 200, 255), 2)
        result = {"stamp": now, "robot": [rx, ry, ryaw], "people": people, "target": target,
                  "back_up": back_up, "back_up_reason": back_reason, "dark": dark,
                  "front_min": None if front_min is None else round(front_min, 3),
                  "hidden": hidden,
                  "state": max((p["state"] for p in people), key=lambda x: STATE_ORDER.get(x, 0))
                  if people else ("unknown" if hidden else "none"),
                  "camera_status": "ok", "observation_valid": not dark,
                  "fall_after_sec": self.fall_after_sec,
                  "partial_fall_after_sec": self.partial_fall_after_sec,
                  "fall_image": fall_image, "fall_reason": fall_reason if fall_image else None}
        return result, frame


# ---------------------------------------------------------------------------
# ROS 2 노드
# ---------------------------------------------------------------------------
class LyingDetectorNode(Node):
    def __init__(self):
        super().__init__("lying_detector")
        p = self.declare_parameter
        p("image_topic", "/image_raw/compressed")
        p("camera_info_topic", "/camera_info")
        p("scan_topic", "/scan")
        p("amcl_topic", "/amcl_pose")
        p("map_frame", "map")          # 로봇 위치: map(AMCL) -> 없으면 odom 좌표계 사용
        p("odom_frame", "odom")
        p("base_frame", "base_footprint")
        p("weights", "")  # 비우면 패키지에 포함된 모델을 사용합니다. 자동 다운로드하지 않습니다.
        p("cam_height", 0.12)
        p("fy", 0.0)               # 0이면 camera_info -> 없으면 vfov로 계산
        p("vfov", 48.8)
        p("tilt", 0.0)
        p("cam_offset_x", 0.07)    # 로봇 중심에서 카메라까지 앞쪽 거리(m)
        p("min_dist", 1.0)
        p("max_dist", 3.0)
        p("vote_min", 10)
        p("vote_ratio", 0.6)
        p("motion_threshold", 0.18)
        p("bbox_motion_threshold", 0.25)
        p("motion_grace_sec", 1.5)
        p("observation_gap_sec", 3.0)
        p("valid_sample_sec", 0.35)
        p("partial_hold_sec", 1.0)
        p("upper_evidence_sec", 2.0)
        p("low_top_height", 0.55)
        p("low_horizontal_min_width", 0.80)
        p("camera_timeout_sec", 1.5)
        p("inference_imgsz", 320)
        p("cpu_threads", 2)
        p("rate", 5.0)            # 초당 처리 횟수
        p("progress_log_interval_sec", 1.0)  # 유효 관찰 시간 출력 간격(초); 0이면 출력 끔
        p("show", True)            # Remote PC에 창 띄우기
        p("save_dir", "~/fall_data/falls")
        p("target_keep_sec", 60.0)  # 같은 자리(1.2m 안)에서 이 시간 안에 다시 보이면 같은 번호 유지
        p("down_keep_sec", 300.0)   # 누워 있던(쓰러진) 사람은 안 보여도 이 시간(초) 동안 기억
        p("fall_after_sec", 5.0)    # 상체 근거가 확인된 경우
        p("partial_fall_after_sec", 6.0)  # 다리 등 신체 일부만 확인된 경우
        p("too_close_dist", 0.40)  # 극근거리+신체 잘림+관절 부족일 때만 '너무 가까움'; 주행 안전정지는 별개
        p("partial_body_enabled", True)  # 어깨/골반이 함께 안 보여도 바닥 근처 부분 신체로 판단
        p("rot_every", 3)          # N프레임마다 화면을 돌려 누운 사람을 한 번 더 찾음 (0이면 끔)
        p("person_conf", PERSON_CONF)  # 사람 검출 신뢰도 기준 (추가 학습 모델은 0.45 추천: 벽·가구 오탐 감소)
        p("lidar_min_len", 1.0)    # 라이다로 이 길이(m) 이상인 바닥 근처 물체 = 누운 사람 후보
        p("lidar_max_len", 2.3)    # 이보다 길면 벽 등으로 보고 제외
        p("map_topic", "/map")     # 지도가 있으면 벽·가구(지도에 있는 것)는 후보에서 뺌
        g = lambda n: self.get_parameter(n).value  # noqa: E731

        fy = g("fy") or None
        self.cam = Camera(g("cam_height"), fy, g("tilt"), g("vfov"))
        weights = (os.path.expanduser(g("weights")) if g("weights") else
                   os.path.join(get_package_share_directory("my_bot_fall"), "models", "fall_pose_n.pt"))
        if not os.path.isfile(weights):
            raise FileNotFoundError(f'모델이 없습니다: {weights}')
        import torch
        torch.set_num_threads(int(g("cpu_threads")))
        cv2.setNumThreads(1)
        self.core = LyingCore(weights, self.cam, g("min_dist"), g("max_dist"), g("vote_min"),
                              g("vote_ratio"), cam_offset_x=g("cam_offset_x"), save_dir=g("save_dir"),
                              rot_every=g("rot_every"), too_close_dist=g("too_close_dist"),
                              fall_after_sec=g("fall_after_sec"), target_keep_sec=g("target_keep_sec"),
                              partial_body_enabled=g("partial_body_enabled"))
        self.core.lidar_min_len, self.core.lidar_max_len = g("lidar_min_len"), g("lidar_max_len")
        self.core.person_conf = g("person_conf")
        self.core.inference_imgsz = int(g("inference_imgsz"))
        self.core.down_keep_sec = g("down_keep_sec")
        self.core.motion_threshold = g("motion_threshold")
        self.core.bbox_motion_threshold = g("bbox_motion_threshold")
        self.core.motion_grace_sec = g("motion_grace_sec")
        self.core.observation_gap_sec = g("observation_gap_sec")
        self.core.valid_sample_sec = g("valid_sample_sec")
        self.core.partial_hold_sec = g("partial_hold_sec")
        self.core.upper_evidence_sec = g("upper_evidence_sec")
        self.core.partial_fall_after_sec = g("partial_fall_after_sec")
        self.core.low_top_height = g("low_top_height")
        self.core.low_horizontal_min_width = g("low_horizontal_min_width")
        self.camera_timeout = g("camera_timeout_sec")
        if min(g("rate"), self.camera_timeout, self.core.observation_gap_sec, self.core.fall_after_sec,
               self.core.partial_fall_after_sec, self.core.partial_hold_sec, self.core.valid_sample_sec, self.core.upper_evidence_sec,
               self.core.motion_grace_sec,
               self.core.low_horizontal_min_width) <= 0 or min(self.core.motion_threshold,
                                                                 self.core.bbox_motion_threshold) < 0:
            raise ValueError("rate/timeouts must be positive; motion_threshold must be nonnegative")
        self.sensor_lock = threading.Lock()
        self.reset_requested = threading.Event()
        self.inference_group = MutuallyExclusiveCallbackGroup()
        self.last_raw_mono = 0.0
        self.last_result_mono = time.monotonic()
        self.inference_started = self.last_result_mono
        self.rejected_image_reason = "waiting"
        self.last_health_reason = None
        self.pending_calibration = None
        self.pending_map = None
        self.last_image_stamp = None
        self.last_received_mono = time.monotonic()
        self.fy_fixed = fy is not None
        self.show = g("show")
        self.progress_log_interval = max(0.0, float(g("progress_log_interval_sec")))
        self._progress_log_at = {}
        self._progress_log_state = {}

        self.latest_img = None
        self.scans = deque(maxlen=20)
        self.robot_pose = None
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.tf_frames = (g("map_frame"), g("odom_frame"), g("base_frame"))
        self.pose_frame = None

        # 토픽 이름이 compressed로 끝나면 압축 영상, 아니면 일반 영상(가제보 /camera/image_raw 등)
        img_type = CompressedImage if g("image_topic").endswith("compressed") else Image
        self.create_subscription(img_type, g("image_topic"), self.on_image, qos_profile_sensor_data)
        self.create_subscription(LaserScan, g("scan_topic"), self.on_scan, qos_profile_sensor_data)
        self.create_subscription(CameraInfo, g("camera_info_topic"), self.on_info, qos_profile_sensor_data)
        amcl_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                              durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(PoseWithCovarianceStamped, g("amcl_topic"), self.on_amcl, amcl_qos)
        self.create_subscription(OccupancyGrid, g("map_topic"), self.on_map, amcl_qos)

        self.pub_state = self.create_publisher(String, "/pose_state", 10)
        self.pub_debug = self.create_publisher(CompressedImage, "/lying_detector/debug/compressed", 1)
        self.create_timer(1.0 / g("rate"), self.tick, callback_group=self.inference_group)
        self.create_timer(0.2, self.watchdog)
        self.start_time = time.time()
        self.last_img_time = 0.0
        self.last_scan_time = 0.0
        self.last_img_warn = 0.0
        self.last_scan_warn = 0.0
        self.frames = 0
        self.fps_t0 = time.time()
        self.get_logger().info(f"누움/쓰러짐 판정 시작: 영상 {g('image_topic')}, 라이다 {g('scan_topic')}")

    @staticmethod
    def stamp_sec(msg):
        return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9

    def on_image(self, msg):
        now = time.monotonic()
        stamp = self.stamp_sec(msg)
        with self.sensor_lock:
            self.last_raw_mono = now
            if stamp > 0 and self.last_image_stamp is not None and stamp <= self.last_image_stamp:
                # Allow a restarted source clock only after a real reception gap.
                if now-self.last_received_mono <= self.camera_timeout:
                    self.rejected_image_reason = 'duplicate_or_old_stamp'
                    return
            if stamp > 0:
                self.last_image_stamp = stamp
            self.last_received_mono = now
            self.latest_img = (msg, now)
            self.last_img_time = time.time()
            self.rejected_image_reason = 'ok'

    def on_scan(self, msg):
        with self.sensor_lock:
            self.scans.append(msg)
            self.last_scan_time = time.time()

    def on_info(self, msg):
        if not self.fy_fixed:
            with self.sensor_lock:
                self.pending_calibration = (msg.width, msg.height, list(msg.k))

    def watchdog(self):
        now = time.monotonic()
        with self.sensor_lock:
            raw_age = now-self.last_raw_mono
            accepted_age = now-self.last_received_mono
            rejected = self.rejected_image_reason
        if raw_age > self.camera_timeout:
            self.publish_unavailable('no_frames')
        elif accepted_age > self.camera_timeout:
            self.publish_unavailable(rejected)
        elif now-max(self.last_result_mono, self.inference_started) > self.camera_timeout:
            self.publish_unavailable('inference_stale')

    def publish_unavailable(self, reason):
        # Do not erase still-time immediately on a brief camera/processing interruption.
        # last_result_mono is updated only for actually processed, healthy images.
        # A gap exceeding observation_gap_sec still resets the observation.
        if time.monotonic() - self.last_result_mono > getattr(self.core, "observation_gap_sec", 3.0):
            self.reset_requested.set()
        # In-flight stale inference must not become a healthy observation.
        self.unavailable_during_inference = True
        if self.last_health_reason != reason:
            self.get_logger().warn(f"판정 불가: {reason} (no_frames=영상 미수신, timestamp_mismatch=시간 불일치, inference_stale=추론 지연)")
            self.last_health_reason = reason
        result = {"stamp": time.time(), "state": "unknown", "camera_status": reason,
                  "observation_valid": False, "people": [], "hidden": [], "target": None,
                  "robot": None, "pose_frame": self.pose_frame, "back_up": False,
                  "back_up_reason": None, "front_min": None, "dark": False,
                  "fall_image": None, "fall_reason": None,
                  "fall_after_sec": self.core.fall_after_sec,
                  "partial_fall_after_sec": getattr(self.core, "partial_fall_after_sec", 6.0)}
        self.pub_state.publish(String(data=json.dumps(result)))

    def image_problem(self, msg, received_mono):
        if time.monotonic()-received_mono > self.camera_timeout:
            return 'frame_processing_delay'
        stamp = self.stamp_sec(msg)
        if stamp > 0:
            age = self.get_clock().now().nanoseconds * 1e-9 - stamp
            if abs(age) > self.camera_timeout:
                return 'timestamp_mismatch'
        return None

    def on_map(self, msg):
        with self.sensor_lock:
            self.pending_map = msg

    def on_amcl(self, msg):
        q = msg.pose.pose.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        self.robot_pose = (msg.pose.pose.position.x, msg.pose.pose.position.y, yaw)

    def lookup_pose(self):
        """TF로 로봇 위치 구하기: map 기준(AMCL 켜짐) -> 없으면 odom 기준 -> 없으면 /amcl_pose 값"""
        mp, od, base = self.tf_frames
        for frame in (mp, od):
            try:
                tr = self.tf_buffer.lookup_transform(frame, base, Time())
            except Exception:
                continue
            t, q = tr.transform.translation, tr.transform.rotation
            yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
            if frame != self.pose_frame:
                self.get_logger().info(f"로봇 위치를 {frame} 좌표계로 계산해요")
                self.pose_frame = frame
            return (t.x, t.y, yaw)
        self.pose_frame = "map" if self.robot_pose is not None else None
        return self.robot_pose

    def closest_scan(self, t):
        """영상 시각과 가장 가까운 라이다"""
        with self.sensor_lock:
            scans = list(self.scans)
        if not scans:
            return None
        return min(scans, key=lambda s: abs(self.stamp_sec(s) - t))

    @staticmethod
    def to_cv(msg):
        """ROS 영상 -> OpenCV BGR 이미지 (cv_bridge 없이)"""
        if isinstance(msg, CompressedImage):
            return cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR)
        enc = msg.encoding.lower()
        ch = {"rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4, "mono8": 1}.get(enc)
        if ch is None:
            return None
        buf = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.step)
        img = buf[:, :msg.width * ch].reshape(msg.height, msg.width, ch)
        conv = {"rgb8": cv2.COLOR_RGB2BGR, "rgba8": cv2.COLOR_RGBA2BGR,
                "bgra8": cv2.COLOR_BGRA2BGR, "mono8": cv2.COLOR_GRAY2BGR}.get(enc)
        return cv2.cvtColor(img, conv) if conv is not None else img.copy()

    def log_still_progress(self, result):
        """순찰/웹 동작에 영향 없이 /pose_state의 유효 관찰 누적값을 터미널에 표시."""
        if self.progress_log_interval <= 0:
            return
        now = time.monotonic()
        for person in result.get("people", []):
            state = person.get("state", "-")
            raw = person.get("raw", "-")
            still = float(person.get("still_sec") or 0.0)
            if raw != "lying" and state not in ("lying", "fell", "fallen") and still <= 0:
                continue  # 주변 정상인까지 매초 출력하지 않음

            pid = person.get("target_id")
            if pid is None:
                pid = f"track-{person.get('track_id', '-')}"
            previous = self._progress_log_state.get(pid)
            if state == "fallen" and previous == "fallen":
                continue  # 확정 결과는 전환 순간 한 번만 출력
            if (state != "fallen" and previous == state and
                    now - self._progress_log_at.get(pid, -1e10) < self.progress_log_interval):
                continue

            mode = person.get("observation_mode") or "none"
            if mode.startswith("upper_"):
                required = float(result.get("fall_after_sec", self.core.fall_after_sec))
                progress = float(person.get("upper_still_sec") or 0.0)
                mode_label = "상체"
            elif mode.startswith("partial_"):
                required = float(result.get("partial_fall_after_sec", self.core.partial_fall_after_sec))
                progress = still
                mode_label = "부분신체"
            else:
                required = float(result.get("partial_fall_after_sec", self.core.partial_fall_after_sec))
                progress = still
                mode_label = "판정대기"

            valid = int(bool(person.get("observation_valid", False)))
            reason = person.get("valid_block_reason") or "-"
            motion_value = person.get("motion")
            motion_text = "-" if motion_value is None else f"{float(motion_value):.3f}"
            if person.get("high_motion_allowed"):
                motion_text += "(큰 움직임·누움 유지 허용)"
            if state == "fallen":
                text = (f"[낙상 판정] 사람#{pid} FALLEN (낙상 의심 확정) | "
                        f"모드={mode_label} 누적={progress:.1f}/{required:g}초 | valid={valid}")
            else:
                text = (f"[낙상 관찰] 사람#{pid} | raw={raw} 상태={state} | valid={valid} | "
                        f"누적={progress:.1f}/{required:g}초 | 모드={mode_label} | "
                        f"움직임={motion_text} | 차단={reason}")
            self.get_logger().info(text)
            self._progress_log_at[pid] = now
            self._progress_log_state[pid] = state

    def tick(self):
        with self.sensor_lock:
            packet, self.latest_img = self.latest_img, None
            calibration, self.pending_calibration = self.pending_calibration, None
            new_map, self.pending_map = self.pending_map, None
        if self.reset_requested.is_set():
            self.reset_requested.clear()
            self.core.reset_observation()
        if calibration:
            self.cam.set_calibration(*calibration)
        if new_map is not None:
            self.core.static_map = new_map
        if packet is None:
            return
        msg, received_mono = packet
        # Fresh inference; watchdog may set the flag again while processing.
        self.unavailable_during_inference = False
        now = time.time()
        problem = self.image_problem(msg, received_mono)
        if problem:
            self.publish_unavailable(problem)
            return
        self.inference_started = time.monotonic()
        try:
            frame = self.to_cv(msg)
        except (ValueError, cv2.error):
            frame = None
        if frame is None:
            self.publish_unavailable("decode_error")
            self.get_logger().warn("영상 변환 실패 (지원 형식: compressed, rgb8, bgr8, rgba8, bgra8, mono8)")
            return
        scan = self.closest_scan(self.stamp_sec(msg))
        since = now - (self.last_scan_time or self.start_time)
        if since > 3 and now - self.last_scan_warn > 5:
            self.get_logger().warn(f"라이다가 {since:.0f}초째 안 들어와요. 발 위치로만 거리를 계산해요.")
            self.last_scan_warn = now
        if since > 1.0:
            scan = None                 # 오래된 라이다 값은 쓰지 않음

        pose = self.lookup_pose()
        self.core.map_ok = self.pose_frame == self.tf_frames[0]
        try:
            result, debug = self.core.process(frame, scan, pose, now)
        except Exception as error:
            self.get_logger().error(f"추론 실패: {error}")
            self.publish_unavailable("inference_error")
            return
        problem = self.image_problem(msg, received_mono)
        if problem or self.reset_requested.is_set() or getattr(self, "unavailable_during_inference", False):
            self.publish_unavailable(problem or "observation_interrupted")
            return
        self.last_result_mono = time.monotonic()
        self.last_health_reason = None
        result["image_stamp"] = self.stamp_sec(msg)
        result["pose_frame"] = self.pose_frame
        self.pub_state.publish(String(data=json.dumps(result, ensure_ascii=False)))
        self.log_still_progress(result)

        if self.pub_debug.get_subscription_count() > 0:
            ok, enc = cv2.imencode(".jpg", debug, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                out = CompressedImage()
                out.header = msg.header
                out.format = "jpeg"
                out.data = enc.tobytes()
                self.pub_debug.publish(out)
        if self.show:
            cv2.imshow("lying detector", debug)
            cv2.waitKey(1)

        # 10초마다 실제 처리 속도 출력 (rate보다 많이 낮으면 n 모델이나 rate 낮추기)
        self.frames += 1
        if now - self.fps_t0 >= 10:
            self.get_logger().info(f"처리 속도 {self.frames / (now - self.fps_t0):.1f} FPS")
            self.frames, self.fps_t0 = 0, now


def main():
    rclpy.init()
    node = LyingDetectorNode()
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
