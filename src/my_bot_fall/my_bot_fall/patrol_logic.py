"""ROS-independent patrol transitions. Observation evidence comes only from fresh pose messages."""
import math
import time


class PatrolLogic:
    def __init__(self, observation_timeout=15.0, cooldown=30.0):
        self.phase = 'setup'
        self.target = None
        self.started = 0.0
        self.clear_since = None
        self.result = None
        self.observation_timeout = observation_timeout
        self.cooldown = cooldown
        self.ignored = []
        self.reason = 'RViz에서 순찰 지점을 지정하세요'
        self.still_sec = 0.0

    @staticmethod
    def same(a, b):
        if not a or not b:
            return False
        if a.get('pose_frame') == b.get('pose_frame') == 'map':
            xy = [a.get('map_x'), a.get('map_y'), b.get('map_x'), b.get('map_y')]
            if all(isinstance(v, (int, float)) and math.isfinite(v) for v in xy):
                return math.hypot(xy[0]-xy[2], xy[1]-xy[3]) < 0.6
        return a.get('target_id') is not None and a.get('target_id') == b.get('target_id')

    def update(self, data, now):
        self.ignored = [(p, until) for p, until in self.ignored if now < until]
        people = [dict(p, pose_frame=data.get('pose_frame')) for p in data.get('people', [])]
        suspects = [p for p in people if p.get('raw') == 'lying' or p.get('state') in ('fell', 'fallen')]
        if self.phase == 'patrol':
            candidate = next((p for p in suspects if not any(self.same(p, q) for q, _ in self.ignored)), None)
            if candidate:
                self.phase, self.target, self.started = 'observe', candidate, now
                self.clear_since, self.result = None, None
                self.reason = '누움 의심: 정지 후 연속 관찰'
        if self.phase != 'observe':
            return
        person = next((p for p in people if self.same(self.target, p)), None)
        if person is None:
            self.clear_since = None
            self.still_sec = 0.0
            self.reason = '대상이 보이지 않아 판단 보류'
            return
        self.still_sec = float(person.get('still_sec', 0))
        if person.get('fallen') and person.get('observation_valid'):
            self.target = person
            self.phase, self.result = 'alert', 'confirmed'
            self.reason = '연속 누움·저움직임 확인: 웹 확인 대기'
        elif person.get('raw') == 'not_lying' and person.get('state') == 'normal':
            self.clear_since = now if self.clear_since is None else self.clear_since
            if now - self.clear_since >= 2.0:
                self.phase, self.target = 'patrol', None
                self.reason = '정상 자세 확인: 순찰 재개'
        else:
            self.clear_since = None
            self.reason = f'정지 관찰 중: 저움직임 {self.still_sec:.1f}초'

    def tick(self, now, pose_ok):
        if not pose_ok:
            self.clear_since = None
            self.still_sec = 0.0
        if self.phase == 'observe' and now-self.started >= self.observation_timeout:
            self.phase, self.result = 'alert', 'uncertain'
            self.reason = '상태 확인 필요: 15초 동안 낙상 여부 미확정 (웹에서 사진 확인)'

    def acknowledge(self):
        if self.phase == 'alert':
            self.phase = 'ack_wait'
            self.reason = '웹 확인 완료: 사람 거리·센서 안전 조건 대기'

    def resume(self, now):
        if self.target:
            self.ignored.append((dict(self.target), now+self.cooldown))
        self.target, self.result = None, None
        self.phase = 'patrol'
        self.reason = '웹 확인 및 안전 조건 충족: 순찰 재개'
