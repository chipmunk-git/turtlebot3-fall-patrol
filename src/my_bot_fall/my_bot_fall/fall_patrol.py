"""Smooth A* + pose subscriber + existing project_3 HTTP API. Only this node drives cmd_vel."""
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.request
import uuid

import cv2
import numpy as np
import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.time import Time
from rclpy.signals import SignalHandlerOptions
from tf2_ros import Buffer, TransformListener
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String, Int32
from geometry_msgs.msg import PoseStamped
from .smooth_base import AStarDriver, world_to_grid
from .patrol_logic import PatrolLogic
from .voice_link import VoiceLink, append_note, voice_note


class FallPatrol(AStarDriver):
    def __init__(self):
        self.localization_seen = 0.0
        self.localization_timeout = 2.0
        self.location_source = "waiting"
        self.amcl_sample = None
        self.raw_rx_seen = 0.0
        self.image_reject = "waiting"
        self.last_block_report = None
        self.logic = PatrolLogic()
        self.voice = VoiceLink()
        self.voice_enabled = False
        self.pose_seen = 0.0
        self.pose_data = {}
        self.pose_stamp = 0.0
        self.pose_timeout = 1.5
        self.server_seen = 0.0
        self.waypoints = []
        self.waypoint_index = 0
        # A route never starts just because the last RViz waypoint was selected.
        # Only an explicit, fresh web /control run command can enable driving.
        self.drive_enabled = False
        self.control_initialized = False
        self.control_epoch = 0
        self.wait_until = None
        self.incident = None
        self.image_jpeg = None
        self.image_seen = 0.0
        # Keep one photo from the CURRENT observation, even if the camera
        # later stops delivering frames before the 25-second alert deadline.
        self.observation_image = None
        self.observation_image_seen = 0.0
        self.observation_started = None
        # Camera/inference interruption fallback: require fresh consecutive lying
        # observations from the CURRENT target and a real cached JPEG.
        self.last_healthy_pose_seen = 0.0
        self.last_observed_lying_seen = 0.0
        self.last_observed_lying_start = 0.0
        self.consecutive_lying_frames = 0
        self.fallback_last_counted_stamp = 0.0
        self.fallback_alert_source = None
        self.worker_stop = threading.Event()
        super().__init__()
        p = lambda n, v: self.declare_parameter(n, v).value
        self.localization_timeout = float(p("localization_timeout_sec", 2.0))
        self.map_frame = p("map_frame", "map")
        self.base_frame = p("base_frame", "base_footprint")
        self.server_url = p('server_url', 'http://127.0.0.1:8000').rstrip('/')
        self.voice_enabled = bool(p('voice_enabled', True))
        self.voice.timeout = float(p('voice_timeout_sec', 90.0))
        if not math.isfinite(self.voice.timeout) or not 1 <= self.voice.timeout <= 120:
            raise ValueError('voice_timeout_sec must be in 1..120')
        self.waypoint_count = int(p('waypoint_count', 3))
        self.wait_seconds = float(p('waypoint_wait_seconds', 2.0))
        self.pose_timeout = float(p('pose_timeout_sec', 1.5))
        self.logic.observation_timeout = float(p('observation_timeout_sec', 15.0))
        self.logic.cooldown = float(p('repeat_alert_cooldown_sec', 30.0))
        self.dropout_alert_sec = float(p('dropout_alert_sec', 3.0))
        self.dropout_min_lying_frames = int(p('dropout_min_lying_frames', 3))
        self.linear_speed = float(p('linear_speed', 0.15))
        if not 2 <= self.waypoint_count <= 20 or not 0 < self.linear_speed <= 0.20:
            raise ValueError('waypoint_count 2..20; linear_speed 0..0.20')
        if (min(self.pose_timeout, self.logic.observation_timeout, self.dropout_alert_sec) <= 0
                or min(self.wait_seconds, self.logic.cooldown) < 0
                or self.dropout_min_lying_frames < 2):
            raise ValueError('Invalid timing parameters')
        self.journal = Path(os.path.expanduser(p('session_file', '~/.turtlewatch/my_bot_fall_session.json')))
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.state_pub = self.create_publisher(String, '/fall_patrol/state', 10)
        self.voice_pub = self.create_publisher(String, '/fall_patrol/voice_request', 10)
        self.create_subscription(String, '/fall_patrol/voice_result', self.on_voice_result, 10)
        self.create_timer(0.5, self.publish_voice_request)
        self.waypoint_pub = self.create_publisher(Int32, '/fall_patrol/waypoint', 10)
        if self.journal.exists():
            saved = json.loads(self.journal.read_text())
            if saved:
                self.incident = saved
                self.logic.phase = 'alert'
                self.logic.result = saved.get('result', 'uncertain')
                self.logic.target = saved.get('target')
                self.logic.reason = '이전 경고 복구: 웹 확인 후 지점 다시 지정'
        self.create_timer(0.2, self.publish_state)
        self.web_thread = threading.Thread(target=self.web_worker, daemon=True)
        self.web_thread.start()
        self.get_logger().info('my_bot_fall: test2 주행 코어 + 자세 모델 1개 + 기존 project_3 웹')
        self.get_logger().info(f'RViz 2D Goal Pose로 {self.waypoint_count}개 지점 지정 → 웹 주행 시작 버튼 대기')

    def setup_estop(self):
        # Override base YOLO worker: the pose node owns the one inference model.
        self.frame_timeout = 1.5
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.create_subscription(CompressedImage, '/image_raw/compressed', self.image_callback, qos,
                                 callback_group=self.cam_group)
        self.create_subscription(String, '/pose_state', self.pose_callback_message, 10,
                                 callback_group=self.cam_group)
        self.pose_guard_group = MutuallyExclusiveCallbackGroup()
        self.create_timer(0.05, self.detect_callback, callback_group=self.pose_guard_group)

    def location_ok(self):
        # Same AMCL position source as the working my_bot driver.
        return self.current_pose is not None

    def image_callback(self, msg):
        self.raw_rx_seen = time.monotonic()
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        if stamp > 0 and abs(self.get_clock().now().nanoseconds*1e-9-stamp) > self.frame_timeout:
            self.image_reject = 'timestamp_mismatch'
            return
        # Health tracks any compressed frame. JPEG is needed only for event snapshots.
        self.last_frame_time = time.monotonic()
        self.image_reject = 'ok'
        if 'jpeg' in msg.format.lower() or 'jpg' in msg.format.lower():
            self.image_jpeg = bytes(msg.data)
            self.image_seen = self.last_frame_time

    def pose_callback_message(self, msg):
        try:
            data = json.loads(msg.data)
            stamp = float(data['stamp'])
            if not math.isfinite(stamp) or stamp <= self.pose_stamp or abs(time.time()-stamp) > self.pose_timeout:
                return
            if not isinstance(data.get('people'), list):
                return
            if any(not isinstance(p, dict) for p in data['people']):
                return
        except (ValueError, KeyError, TypeError):
            return
        with self.command_lock:
            self.pose_stamp = stamp
            self.pose_data = data
            self.pose_seen = time.monotonic()
            self.det_frame_time = self.pose_seen
            if self.pose_is_healthy():
                self.last_healthy_pose_seen = self.pose_seen
                previous_phase = self.logic.phase
                self.logic.update(data, self.pose_seen)
                if (self.voice_enabled and previous_phase == 'patrol'
                        and self.logic.phase in ('observe', 'alert')):
                    self.voice.start(time.time())
                if self.logic.phase == 'observe':
                    if previous_phase != 'observe':
                        # Never attach a photo from the previous person/event.
                        self.clear_observation_image()
                        self.observation_started = self.logic.started
                    self.capture_observation_image()
                    self.update_dropout_evidence()
                elif previous_phase == 'observe' and self.logic.phase == 'patrol':
                    self.clear_observation_image()
            if self.logic.phase != 'patrol':
                self.stop_robot()

    def sync_voice(self):
        if not self.voice_enabled:
            return
        if self.logic.phase not in ('observe', 'alert'):
            self.voice.end()
            return
        self.voice.message(time.time())
        if (self.incident is not None and self.voice.request and self.voice.result
                and self.incident.get('voice_request_id') == self.voice.request['request_id']
                and self.incident.get('voice_result') != self.voice.result):
            self.incident['voice_result'] = dict(self.voice.result)
            self.save_incident()

    def publish_voice_request(self):
        if not self.voice_enabled:
            return
        with self.command_lock:
            self.sync_voice()
            message = self.voice.message(time.time())
        if message:
            self.voice_pub.publish(String(data=json.dumps(message, ensure_ascii=False)))

    def on_voice_result(self, message):
        if not self.voice_enabled or len(message.data) > 16000:
            return
        try:
            data = json.loads(message.data)
        except (TypeError, ValueError):
            return
        with self.command_lock:
            if self.logic.phase in ('observe', 'alert') and self.voice.accept(data, time.time()):
                self.sync_voice()

    def clear_observation_image(self):
        self.observation_image = None
        self.observation_image_seen = 0.0
        self.observation_started = None
        self.last_observed_lying_seen = 0.0
        self.last_observed_lying_start = 0.0
        self.consecutive_lying_frames = 0
        self.fallback_last_counted_stamp = 0.0
        self.fallback_alert_source = None

    def update_dropout_evidence(self):
        """Only recent *new* healthy lying frames from the watched person count.

        Never count repeated/stale images, an upright/unknown posture, a different
        person or an absent target. This records evidence, not elapsed fall time.
        """
        if self.logic.phase != 'observe' or self.logic.target is None:
            return
        frame = self.pose_data.get('pose_frame')
        watched = next((p for p in self.pose_data.get('people', [])
                        if self.logic.same(self.logic.target,
                                           dict(p, pose_frame=frame))), None)
        raw_lying = watched is not None and watched.get('raw') == 'lying'
        if not raw_lying:
            self.consecutive_lying_frames = 0
            self.last_observed_lying_seen = 0.0
            self.last_observed_lying_start = 0.0
            return
        # Must be a strictly new pose message; previous pose_stamp is set in
        # pose_callback_message and validates strictly increasing stamps.
        if self.fallback_last_counted_stamp >= self.pose_stamp:
            return
        self.fallback_last_counted_stamp = self.pose_stamp
        if (not self.last_observed_lying_seen or
                self.pose_seen - self.last_observed_lying_seen > 1.0):
            self.consecutive_lying_frames = 0
            self.last_observed_lying_start = self.pose_seen
        self.consecutive_lying_frames += 1
        self.last_observed_lying_seen = self.pose_seen

    def confirm_on_observation_dropout(self, now):
        """Alert from a cached *past* lying photo after real observation loss.

        An interruption is not 6 seconds of continuous observation: the event
        is a low-confidence 'confirmed' web alert, annotated as camera/inference
        dropout. This method must run under command_lock.
        """
        if self.logic.phase != 'observe' or self.incident is not None:
            return False
        if self.consecutive_lying_frames < self.dropout_min_lying_frames:
            return False
        if (self.observation_image is None or
                self.observation_started != self.logic.started):
            return False
        # Recent lying proof must be the last valid observation near the outage;
        # do not resurrect a person from an old cached picture.
        if (self.last_healthy_pose_seen <= 0 or self.last_observed_lying_seen <= 0 or
                self.last_healthy_pose_seen - self.last_observed_lying_seen > 1.0):
            return False
        # Recovered healthy pose messages cancel the dropout timer.
        if self.pose_is_healthy():
            return False
        if now - self.last_healthy_pose_seen < self.dropout_alert_sec:
            return False
        # Do not trigger on a momentary loss before the fallback window;
        # a long-disconnected robot should request human checking via timeout.
        if now - self.last_healthy_pose_seen > self.logic.observation_timeout:
            return False
        self.fallback_alert_source = 'observation_dropout_snapshot'
        self.logic.phase = 'alert'
        self.logic.result = 'confirmed'
        self.logic.reason = ('영상/자세 판정 수신 중단: 마지막 누운 자세 사진으로 '
                             '낙상 의심 경고 (실제 낙상 확정 아님)')
        self.get_logger().warn(
            f'영상/자세 판정 {now - self.last_healthy_pose_seen:.1f}초 중단: '
            f'최근 누움 {self.consecutive_lying_frames}회 + 저장된 사진 -> 낙상 의심 경고')
        return True

    def capture_observation_image(self):
        """Cache a recent JPEG only while the watched person is still visible.

        Called from the fresh pose callback (5 FPS), not the camera's 30 FPS
        callback, to avoid adding per-frame CPU work to the video pipeline.
        """
        if self.logic.phase != 'observe' or self.image_jpeg is None:
            return
        if not (0 <= self.pose_seen - self.image_seen < self.frame_timeout):
            return
        if self.observation_started != self.logic.started:
            return
        frame = self.pose_data.get('pose_frame')
        people = self.pose_data.get('people', [])
        watched = any(
            self.logic.same(self.logic.target, dict(person, pose_frame=frame))
            and (person.get('raw') == 'lying' or
                 person.get('state') in ('lying', 'fell', 'fallen') or
                 person.get('fallen') is True)
            for person in people
        )
        if not watched:
            return
        # bytes are already copied by image_callback; a reference is enough.
        first = self.observation_image is None
        self.observation_image = self.image_jpeg
        self.observation_image_seen = self.image_seen
        if first:
            self.get_logger().info('관찰 중 사진 확보: 확인 필요/낙상 경고에 첨부 예정')

    def pose_is_healthy(self):
        return (time.monotonic()-self.pose_seen <= self.pose_timeout
                and abs(time.time()-self.pose_stamp) <= self.pose_timeout
                and self.pose_data.get('camera_status') == 'ok'
                and self.pose_data.get('observation_valid') is True)

    def detect_callback(self):
        with self.command_lock:
            now = time.monotonic()
            self.camera_stale = now-self.last_frame_time > self.frame_timeout
            self.inference_stale = not self.pose_is_healthy()
            # Posture visibility flags do not represent collision distance.
            near = self.pose_is_healthy() and any(
                p.get('dist_src') == 'lidar'
                and isinstance(p.get('dist'), (int, float))
                and math.isfinite(p['dist']) and 0 < p['dist'] <= self.estop_distance_threshold
                for p in self.pose_data.get('people', []))
            if near:
                self.last_detect_time = now
                self.estop_active = True
            elif self.pose_is_healthy() and now-self.last_detect_time >= self.estop_release_delay:
                self.estop_active = False
            if self.motion_blocked():
                self.stop_robot()

    def motion_blocked(self):
        return (not self.drive_enabled or self.logic.phase != 'patrol' or not self.location_ok() or not self.pose_is_healthy()
                or time.monotonic()-self.server_seen > 3.0 or super().motion_blocked())

    def goal_callback(self, msg):
        with self.command_lock:
            if self.logic.phase != 'setup':
                self.get_logger().warn('순찰·경고 중 지점 변경 불가. 재실행 후 지정하세요.')
                return
            if msg.header.frame_id not in ('', 'map') or self.base_blocked is None:
                self.get_logger().warn('map 수신 후 map 좌표계로 지점을 지정하세요.')
                return
            point = (float(msg.pose.position.x), float(msg.pose.position.y))
            if not all(math.isfinite(v) for v in point):
                return
            r, c = world_to_grid(*point, self.map_origin, self.map_resolution)
            h, w = self.base_blocked.shape
            if not (0 <= r < h and 0 <= c < w) or self.base_blocked[r, c]:
                self.get_logger().warn('벽 여유 영역 또는 지도 밖입니다. 다른 지점을 선택하세요.')
                return
            if any(math.dist(point, q) < 0.25 for q in self.waypoints):
                self.get_logger().warn('지점 간격은 25cm 이상으로 지정하세요.')
                return
            self.waypoints.append(point)
            self.get_logger().info(f'{len(self.waypoints)}/{self.waypoint_count} 지점: {point}')
            if len(self.waypoints) == self.waypoint_count:
                self.logic.phase = 'ready'
                self.logic.reason = '순찰 준비 완료: 웹 주행 시작 버튼을 누르세요'
                self.pause_drive(self.logic.reason, reset_control=True)
                self.get_logger().info('목적지 설정 완료. 웹의 주행 시작 버튼을 누르기 전까지 정지 유지')

    def set_patrol_goal(self):
        msg = PoseStamped()
        msg.header.frame_id = 'map'
        msg.pose.position.x, msg.pose.position.y = self.waypoints[self.waypoint_index]
        msg.pose.orientation.w = 1.0
        super().goal_callback(msg)

    def pause_drive(self, reason, reset_control=False):
        """Stop motion without discarding waypoints, observation, or voice."""
        with self.command_lock:
            if self.drive_enabled and self.wait_until is not None and self.waypoints:
                self.waypoint_index = (self.waypoint_index + 1) % len(self.waypoints)
            self.drive_enabled = False
            self.wait_until = None
            self.cancel_goal() if self.goal_pose is not None else self.stop_robot()
            if self.logic.phase == 'patrol':
                self.logic.phase = 'ready'
                self.logic.reason = reason
            if reset_control:
                self.control_initialized = False
                # Invalidate a GET/PUT already in flight on the web thread.
                self.control_epoch += 1

    def apply_drive_command(self, drive):
        """Apply an explicit web command. Return False for rejected early RUN.

        No driving after restart, no old goal after STOP, and no unattended
        restart after a cleared fall alert. Only the patrol node owns /cmd_vel.
        """
        with self.command_lock:
            if drive == 'stop':
                if self.drive_enabled or self.goal_pose is not None:
                    self.get_logger().info('웹 정지 명령 수신: 로봇 정지, 기존 목적지 3곳 유지')
                self.pause_drive('웹에서 주행 중지: 다시 시작 버튼 대기')
                return True
            if drive != 'run':
                return False
            if self.logic.phase in ('alert', 'ack_wait', 'error') or self.incident is not None:
                return False
            if self.drive_enabled:
                return True
            if (len(self.waypoints) != self.waypoint_count
                    or self.logic.phase != 'ready' or self.incident is not None):
                self.get_logger().warn('주행 시작 거부: 목적지 3곳 설정 및 경고 해제 후 다시 눌러주세요')
                return False
            # Keep the previously selected waypoint after a web STOP.
            # set_patrol_goal() replans a fresh A* path from the robot's
            # current AMCL position to that SAME destination.
            self.wait_until = None
            self.drive_enabled = True
            self.logic.phase = 'patrol'
            self.logic.reason = '웹에서 주행 시작/재개'
            self.set_patrol_goal()
            self.get_logger().info(
                f'웹 주행 시작/재개: {self.waypoint_index + 1}번 목적지를 향해 현재 위치에서 경로 재계산')
            return True

    def publish_smooth(self, v, w, stationary=False):
        # Base A* checks its own obstacle guard but does not know about web STOP.
        # Guard the final velocity publisher too so no stale nonzero command
        # can slip through after STOP has been applied.
        with self.command_lock:
            if not self.drive_enabled or self.logic.phase != 'patrol':
                self.stop_robot()
                return
            super().publish_smooth(v, w, stationary=stationary)

    def save_incident(self):
        data = {k:v for k,v in (self.incident or {}).items() if k != 'image'}
        tmp = self.journal.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.journal)

    def make_incident(self):
        target = self.logic.target or {}
        is_map = target.get('pose_frame') == 'map'
        # Prefer a frame actually captured while this person was observed.
        # The latest live frame may be missing or show an empty scene after a
        # 25-second 'uncertain' observation. No old observation is reused.
        from_observation = (self.observation_image is not None and
                            self.observation_started == self.logic.started)
        current_usable = (self.image_jpeg is not None and
                          0 <= time.monotonic()-self.image_seen < self.frame_timeout)
        snapshot = (self.observation_image if from_observation else
                    self.image_jpeg if current_usable else None)
        self.incident = dict(token=str(uuid.uuid4()), event_id=None,
                             result=self.logic.result or 'uncertain', target=target,
                             fallback_source=self.fallback_alert_source,
                             x=target.get('map_x') if is_map else None,
                             y=target.get('map_y') if is_map else None,
                             image=snapshot)
        if self.voice_enabled and self.voice.request:
            self.incident['voice_request_id'] = self.voice.request['request_id']
            if self.voice.result:
                self.incident['voice_result'] = dict(self.voice.result)
        if from_observation:
            self.get_logger().info('경고 사진 첨부: 관찰 중 저장한 카메라 사진')
        elif current_usable:
            self.get_logger().info('경고 사진 첨부: 최근 카메라 사진 (관찰 사진 없음)')
        else:
            self.get_logger().warn('경고 사진 첨부 불가: 관찰 중 JPEG 없음 / 최신 영상 없음')
        self.clear_observation_image()
        try:
            self.save_incident()
        except OSError as e:
            self.logic.phase, self.logic.reason = 'error', f'경고 기록 저장 실패: {e}'

    def control_loop(self):
        with self.command_lock:
            now = time.monotonic()
            if self.drive_enabled and now-self.server_seen > 3.0:
                self.pause_drive('웹 연결 중단: 복구 후 시작 버튼 대기', reset_control=True)
            self.confirm_on_observation_dropout(now)
            self.logic.tick(now, self.pose_is_healthy())
            self.sync_voice()
            if self.logic.phase == 'alert' and self.incident is None:
                self.pause_drive('경고 발생: 웹 확인 및 시작 버튼 대기', reset_control=True)
                self.make_incident()
            if self.logic.phase == 'ack_wait':
                self.stop_robot()
                if self.location_ok() and self.pose_is_healthy() and not super().motion_blocked() and now-self.server_seen <= 3.0:
                    previous = self.incident
                    self.incident = None
                    try:
                        self.save_incident()
                    except OSError:
                        self.incident = previous
                        self.logic.phase = 'error'
                        self.logic.reason = '경고 해제 기록 실패'
                        return
                    self.logic.resume(now)
                    self.pause_drive('경고 확인 완료: 웹 주행 시작 버튼 대기', reset_control=True)
                    self.clear_observation_image()
                    if not self.waypoints:
                        self.logic.phase = 'setup'
                    elif not self.drive_enabled:
                        self.logic.phase = 'ready'
                        self.logic.reason = '경고 확인 완료: 웹 주행 시작 버튼 대기'
                return
            if self.logic.phase == 'patrol' and not self.drive_enabled:
                self.logic.phase = 'ready'
                self.logic.reason = '웹에서 주행 중지: 다시 시작 버튼 대기'
            if self.logic.phase != 'patrol' or not self.drive_enabled:
                self.stop_robot()
                return
            if self.wait_until is not None:
                self.stop_robot()
                if self.motion_blocked() or now < self.wait_until:
                    return
                self.wait_until = None
                self.waypoint_index = (self.waypoint_index+1) % len(self.waypoints)
                self.set_patrol_goal()
                return
            before = self.goal_pose
            arrived = (before is not None and self.current_pose is not None and
                       math.dist(before, self.current_pose) < self.goal_tolerance)
            # Keep planning, velocity publishing and web STOP serialized.
            # Otherwise a concurrent STOP can be followed by an old velocity.
            super().control_loop()
            if self.logic.phase == 'patrol' and self.drive_enabled and before is not None and self.goal_pose is None:
                if arrived:
                    self.wait_until = now+self.wait_seconds
                else:
                    self.logic.phase, self.logic.reason = 'error', '경로 생성 실패: 지점/초기 위치 확인'
                    self.stop_robot()

    def public_state(self):
        if self.logic.phase in ('alert', 'ack_wait', 'error'):
            return 'alert'
        if self.logic.phase == 'observe':
            return 'observe'
        return 'patrol' if self.logic.phase == 'patrol' and not self.motion_blocked() and self.wait_until is None else 'idle'

    def block_reasons(self):
        now = time.monotonic()
        reasons = []
        if self.logic.phase != 'patrol':
            reasons.append(self.logic.reason)
        if not self.location_ok():
            reasons.append('AMCL 위치 수신 대기: 초기 위치를 지정하세요')
        if now-self.scan_time > self.scan_timeout:
            reasons.append('라이다 수신 지연')
        elif self.all_dist <= self.stop_dist:
            reasons.append(f'장애물 안전거리 이내 ({self.all_dist:.2f}m)')
        if self.estop_active:
            reasons.append('라이다 거리로 확인된 가까운 사람')
        if now-self.last_frame_time > self.frame_timeout:
            reasons.append('영상 시각 불일치: 로봇/PC 시간 확인' if self.image_reject == 'timestamp_mismatch'
                           else '카메라 영상 수신 지연')
        if not self.pose_is_healthy():
            reasons.append('자세 판정 수신 불량: '+str(self.pose_data.get('camera_status', 'waiting')))
        if now-self.server_seen > 3.0:
            reasons.append('웹 서버 연결 대기/끊김')
        if self.vision_fault:
            reasons.append('비전 오류')
        return reasons

    def publish_state(self):
        with self.command_lock:
            report = ' / '.join(self.block_reasons())
            if report != self.last_block_report:
                self.get_logger().info('정지 조건: '+report if report else '출발 조건 충족')
                self.last_block_report = report
            self.state_pub.publish(String(data=self.public_state()))
            self.waypoint_pub.publish(Int32(data=self.waypoint_index+1 if self.waypoints else 0))

    def http(self, method, path, data=None, content_type='application/json'):
        body = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.server_url+path, data=body, method=method)
        if body is not None:
            request.add_header('Content-Type', content_type)
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:
                return json.loads(response.read())
        except (OSError, ValueError):
            self.pause_drive('웹 연결 오류: 복구 후 시작 버튼 대기', reset_control=True)
            raise

    def post_incident(self, incident):
        boundary = uuid.uuid4().hex
        fields = {'status':incident['result'], 'x':incident['x'], 'y':incident['y']}
        body = b''
        for name, value in fields.items():
            if value is not None:
                body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        image = incident.get('image')
        if image:
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="detected_snapshot"; filename="fall_suspect.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()+image+b'\r\n')
        body += f'--{boundary}--\r\n'.encode()
        return self.http('POST', '/events', body, 'multipart/form-data; boundary='+boundary)

    def poll_drive_control(self):
        """Control faults block motion, but must not block event/voice reporting."""
        try:
            with self.command_lock:
                if self.drive_enabled and time.monotonic()-self.server_seen > 3.0:
                    self.pause_drive('웹 연결 중단: 복구 후 시작 버튼 대기', reset_control=True)
                epoch = self.control_epoch
                initialized = self.control_initialized
            if not initialized:
                # Consume neither a saved RUN nor a click made before recovery.
                reset = self.http('PUT', '/control', {'drive': 'stop'})
                if not isinstance(reset, dict) or reset.get('drive') != 'stop':
                    raise ValueError('주행 정지 초기화 응답 불일치')
                with self.command_lock:
                    if epoch == self.control_epoch:
                        self.control_initialized = True
                return
            control = self.http('GET', '/control')
            if not isinstance(control, dict) or control.get('drive') not in ('run', 'stop'):
                raise ValueError('잘못된 주행 명령 응답')
            with self.command_lock:
                if epoch != self.control_epoch or not self.control_initialized:
                    return
                if self.drive_enabled and time.monotonic()-self.server_seen > 3.0:
                    self.pause_drive('웹 연결 중단: 복구 후 시작 버튼 대기', reset_control=True)
                elif not self.apply_drive_command(control['drive']):
                    self.pause_drive('주행 시작 거부: 준비 후 다시 시작하세요', reset_control=True)
        except Exception as error:
            self.pause_drive('주행 명령 오류: 복구 후 시작 버튼 대기', reset_control=True)
            self.get_logger().warn(f'주행 명령 대기: {error}', throttle_duration_sec=5.0)

    def web_cycle(self):
        self.http('GET', '/health')
        self.poll_drive_control()
        with self.command_lock:
            incident = self.incident
            pose = self.current_pose if self.location_ok() else None
            status = dict(state=self.public_state(), x=pose[0] if pose else None,
                          y=pose[1] if pose else None, yaw=self.current_yaw if pose else None,
                          current_waypoint=self.waypoint_index+1 if self.waypoints else None)
        self.http('PUT', '/status', status)
        self.server_seen = time.monotonic()
        if incident and self.logic.phase in ('alert', 'ack_wait'):
            if incident.get('event_id') is None:
                if incident.get('creating'):
                    self.logic.phase = 'error'
                    self.logic.reason = '이벤트 생성 응답 불명: 웹 기록 확인 필요'
                    raise RuntimeError(self.logic.reason)
                with self.command_lock:
                    incident['creating'] = True
                    self.save_incident()
                try:
                    result = self.post_incident(incident)
                except Exception:
                    with self.command_lock:
                        self.logic.phase = 'error'
                        self.logic.reason = '이벤트 전송 실패: 중복 생성을 막고 정지 유지'
                    raise
                with self.command_lock:
                    incident['event_id'] = int(result['id'])
                    self.save_incident()
            if not incident.get('note_sent'):
                if incident.get('fallback_source') == 'observation_dropout_snapshot':
                    note = ('낙상 의심(영상 끊김): 관찰 중 누운 자세가 반복 확인됐지만 '
                            '카메라/자세 판정 수신이 3초 이상 중단되었습니다. '
                            '마지막 정상 관찰 사진을 첨부했습니다. 현재 상태는 미확인입니다. '
                            '사용자가 사진과 현장을 확인해 주세요.')
                else:
                    note = ('연속 누움·저움직임 기반 낙상 의심. 실제 사고 확정 아님.'
                            if incident['result'] == 'confirmed' else '상태 확인 필요: 누운 자세 의심을 감지했으나 15초 안에 낙상 여부를 확정하지 못했습니다. 관찰 중 사진을 확인해 주세요. (확인 필요 알림)')
                self.append_event_note(incident['event_id'], '[robot:' + incident['token'] + ']', note)
                with self.command_lock:
                    incident['note_sent'] = True
                    self.save_incident()
            voice_result = incident.get('voice_result')
            if voice_result and not incident.get('voice_note_sent'):
                self.append_event_note(incident['event_id'],
                                       '[voice:' + voice_result['request_id'] + ']',
                                       voice_note(voice_result))
                with self.command_lock:
                    incident['voice_note_sent'] = True
                    self.save_incident()
            event = self.http('GET', f"/events/{incident['event_id']}")
            event_status = event.get('status')
            # New GUI: '조치 완료' (resolved) and '오탐' (false_alarm) are
            # terminal user actions; there is no separate '확인' step.
            # Legacy 'acknowledged' also remains supported for old events.
            manually_cleared = (event_status in ('resolved', 'false_alarm') or
                                (event_status == 'acknowledged' and
                                 bool(event.get('acknowledged_at'))))
            if manually_cleared:
                with self.command_lock:
                    if self.incident is incident:
                        self.logic.acknowledge()
                        if event_status == 'false_alarm' and self.logic.phase == 'ack_wait':
                            self.logic.reason = '웹 오탐 처리: 사람 거리·센서 안전 조건 대기'

    def append_event_note(self, event_id, marker, text):
        event = self.http('GET', f'/events/{event_id}')
        previous = event.get('note') or ''
        note = append_note(previous, marker, text)
        if note != previous:
            self.http('PATCH', f'/events/{event_id}', {'note': note})

    def web_worker(self):
        while not self.worker_stop.is_set():
            try:
                self.web_cycle()
            except Exception as error:
                self.pause_drive('웹 전송 오류: 복구 후 시작 버튼 대기', reset_control=True)
                self.get_logger().warn(f'웹 연결/전송 대기: {error}', throttle_duration_sec=5.0)
            self.worker_stop.wait(0.5)


def main(args=None):
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = FallPatrol()
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.closing = True
        node.stop_robot()
        with node.command_lock:
            node.voice.end()
        node.publish_voice_request()
        node.worker_stop.set()
        executor.shutdown(timeout_sec=2.0)
        node.web_thread.join(timeout=3.0)
        node.close_camera()
        node.destroy_node()
        rclpy.try_shutdown()
