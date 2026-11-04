"""Best-effort dashboard logs and the rules for what reaches the dashboard. No ROS.

Sending runs on its own thread and never raises: a slow or missing server must not
affect driving, unlike FallPatrol.http() whose failures deliberately pause the robot.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque

# logic.reason prefix -> (category, level)
REASON_LOGS = (
    ('누움 의심', 'vision', 'warn'),
    ('연속 누움·저움직임 확인', 'vision', 'error'),
    ('상태 확인 필요', 'vision', 'warn'),
    ('정상 자세 확인', 'vision', 'ok'),
    ('영상/자세 판정 수신 중단', 'vision', 'error'),
    ('이전 경고 복구', 'event', 'warn'),
    ('이벤트 생성 응답 불명', 'event', 'error'),
    ('이벤트 전송 실패', 'event', 'error'),
    ('경고 기록 저장 실패', 'event', 'error'),
    ('경고 해제 기록 실패', 'event', 'error'),
    ('경로 생성 실패', 'route', 'error'),
)

# ROS node -> (message pattern, category, level), read from /rosout.
ROSOUT_LOGS = {
    'astar_driver_yolo': (
        (r'\d+/\d+ 지점', 'route', 'info'),
        (r'목적지 설정 완료', 'route', 'ok'),
        (r'벽 여유 영역 또는 지도 밖|지점 간격은|map 수신 후', 'route', 'warn'),
        (r'순찰·경고 중 지점 변경 불가', 'route', 'warn'),
        (r'경로를 찾지 못했습니다|목표가 벽 근처|아직 지도를|아직 /amcl_pose', 'route', 'warn'),
        (r'현재 위치가 벽 여유 영역', 'route', 'warn'),
        (r'주행 시작 거부', 'drive', 'warn'),
        (r'영상 디코딩/YOLO 오류', 'vision', 'error'),
        (r'경고 사진 첨부 불가', 'event', 'warn'),
        (r'경고 음성 재생 실패', 'voice', 'error'),
    ),
    'lying_detector': ((r'추론 실패', 'vision', 'error'),),
    'tb3_arduino_bridge': ((r'Serial connection lost|Unable to open', 'output', 'error'),),
}


def classify_reason(reason):
    for prefix, category, level in REASON_LOGS:
        if reason.startswith(prefix):
            return category, level
    return None


def classify_rosout(node, text):
    for pattern, category, level in ROSOUT_LOGS.get(node, ()):
        if re.match(pattern, text):
            return category, level
    return None


def block_key(reason):
    """One name per condition: the distance or the detector's detail may change while it lasts."""
    if reason.startswith('자세 판정 수신 불량'):
        return '자세 판정 수신 불량'
    return re.sub(r' \([^()]*\)$', '', reason)


class BlockTracker:
    """Turn the stop conditions into what a person needs to read.

    A condition is reported only once it has lasted `hold` seconds, and cleared only once it
    has been gone that long. Shorter stops on patrol become one summary line per `window`.
    """

    def __init__(self, hold=3.0, window=30.0):
        self.hold = hold
        self.window = window
        self.conditions = {}
        self.ready = False
        self.patrolling = False
        self.short_stops = {}
        self.summary_at = None

    def update(self, reasons, patrolling, now):
        current = {block_key(reason): reason for reason in reasons}
        changes, cleared = [], []
        for key, text in current.items():
            condition = self.conditions.setdefault(key, dict(since=now, reported=False))
            condition['gone'] = None
            if not condition['reported'] and now-condition['since'] >= self.hold:
                condition['reported'] = True
                changes.append(('warn', '정지: ' + text))
        for key, condition in list(self.conditions.items()):
            if key in current:
                continue
            if not condition['reported']:
                del self.conditions[key]
                if patrolling:
                    self.short_stops[key] = self.short_stops.get(key, 0) + 1
                continue
            condition['gone'] = condition['gone'] if condition['gone'] is not None else now
            if now-condition['gone'] >= self.hold:
                del self.conditions[key]
                cleared.append(key)
        changes += self._summary(patrolling, now)
        reported = any(condition['reported'] for condition in self.conditions.values())
        ready = patrolling and not reported and (self.ready or not self.conditions)
        if ready and not self.ready:
            suffix = ' (해소: ' + ', '.join(cleared) + ')' if cleared else ''
            changes.append(('ok', '출발 조건 충족' + suffix))
        else:
            changes += [('info', '해소: ' + key) for key in cleared]
        self.ready, self.patrolling = ready, patrolling
        return changes

    def _summary(self, patrolling, now):
        if self.summary_at is None or (patrolling and not self.patrolling):
            self.summary_at = now
            self.short_stops = {}
        if not self.short_stops or (patrolling and now-self.summary_at < self.window):
            if not patrolling:
                self.summary_at = now
            return []
        counts = ', '.join(f'{key} {count}회' for key, count in self.short_stops.items())
        elapsed = max(1, round(now-self.summary_at))
        self.summary_at, self.short_stops = now, {}
        return [('warn', f'최근 {elapsed}초간 짧은 정지: {counts}')]


class WaypointTracker:
    """Say where the patrol is heading, once per leg rather than once per sensor hiccup."""

    def __init__(self):
        self.heading = None

    def update(self, index, total, driving, waiting):
        """index is zero-based. driving: on patrol with the web start on. waiting: at a waypoint."""
        if not total:
            self.heading = None
            return []
        heading = index + 1 if driving and not waiting else None
        changes = []
        if waiting and self.heading is not None:
            changes.append(('ok', f'{self.heading}/{total}번 지점 도착'))
        if heading is not None and heading != self.heading:
            changes.append(('info', f'{heading}/{total}번 지점으로 이동'))
        self.heading = heading
        return changes


class WebLog:
    def __init__(self, server_url, source, window=30.0, post=None, clock=time.monotonic,
                 wall=time.time, start=True):
        self.url = server_url.rstrip('/') + '/logs'
        self.source = source
        self.window = window
        self.post = post or self._post
        self.clock = clock
        self.wall = wall
        self.lock = threading.Lock()
        self.queue = deque(maxlen=200)
        self.seen = {}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True) if start else None
        if self.thread:
            self.thread.start()

    def add(self, level, category, message, key=None, collapse=True):
        """Queue a log. Repeats of the same key inside the window become one count."""
        key = key or message
        now = self.clock()
        with self.lock:
            if self.stop_event.is_set():
                return
            self._expire(now)
            if not collapse:
                self._enqueue(level, category, message)
                return
            seen = self.seen.get(key)
            if seen:
                seen['count'] += 1
                return
            self.seen[key] = dict(first=now, count=0, level=level, category=category, message=message)
            self._enqueue(level, category, message)

    def _enqueue(self, level, category, message):
        self.queue.append(dict(at=self.wall(), level=level, category=category, message=message))

    def _expire(self, now):
        for key, seen in list(self.seen.items()):
            if now - seen['first'] >= self.window:
                del self.seen[key]
                if seen['count']:
                    self._enqueue(seen['level'], seen['category'],
                                  f"{seen['message']} (최근 {self.window:.0f}초간 {seen['count']}회 더)")

    def pump(self):
        """Send what is queued. Returns False when the server cannot be reached."""
        with self.lock:
            self._expire(self.clock())
        while not self.stop_event.is_set():
            with self.lock:
                item = self.queue[0] if self.queue else None
            if item is None:
                return True
            try:
                delivered = self.post(self._body(item))
            except Exception:
                delivered = False
            if not delivered:
                return False
            with self.lock:
                if self.queue and self.queue[0] is item:
                    self.queue.popleft()
        return True

    def _body(self, item):
        message = item['message']
        if self.wall() - item['at'] > 5.0:
            message += time.strftime(' (%H:%M:%S 발생)', time.localtime(item['at']))
        return dict(source=self.source, level=item['level'], category=item['category'], message=message[:1000])

    def _post(self, body):
        try:
            request = urllib.request.Request(self.url, data=json.dumps(body).encode(), method='POST',
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=1.0):
                return True
        except urllib.error.HTTPError as error:
            # A rejected log or an older server will not improve on retry: drop it.
            return error.code < 500
        except (OSError, ValueError):
            return False

    def _run(self):
        while not self.stop_event.is_set():
            self.stop_event.wait(0.5 if self.pump() else 2.0)

    def stop(self, timeout=1.0):
        """Bound shutdown even if delivery is stuck; pending logs are best effort."""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=timeout)
