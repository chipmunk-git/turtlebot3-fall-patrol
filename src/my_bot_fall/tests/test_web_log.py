"""Dashboard log rules and delivery. No ROS, robot, or server."""
import importlib.util
import json
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("web_log", Path(__file__).parents[1] / "my_bot_fall" / "web_log.py")
WEB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WEB)


class RuleTests(unittest.TestCase):
    def test_selected_reasons_are_logged_and_progress_is_not(self):
        self.assertEqual(WEB.classify_reason("누움 의심: 정지 후 연속 관찰"), ("vision", "warn"))
        self.assertEqual(WEB.classify_reason("이벤트 전송 실패: 중복 생성을 막고 정지 유지"), ("event", "error"))
        self.assertIsNone(WEB.classify_reason("정지 관찰 중: 저움직임 3.0초"))
        self.assertIsNone(WEB.classify_reason("RViz에서 순찰 지점을 지정하세요"))

    def test_rosout_rules_match_by_node_and_message(self):
        self.assertEqual(WEB.classify_rosout("astar_driver_yolo", "2/3 지점: (0.1, 0.2)"), ("route", "info"))
        self.assertEqual(WEB.classify_rosout("tb3_arduino_bridge", "Serial connection lost: x"), ("output", "error"))
        self.assertEqual(WEB.classify_rosout("lying_detector", "추론 실패: x"), ("vision", "error"))
        self.assertIsNone(WEB.classify_rosout("astar_driver_yolo", "출발 조건 충족"))
        self.assertIsNone(WEB.classify_rosout("other_node", "추론 실패: x"))


class BlockTrackerTests(unittest.TestCase):
    def test_lasting_stop_is_reported_once_then_cleared(self):
        tracker = WEB.BlockTracker()
        self.assertEqual(tracker.update([], True, 0), [("ok", "출발 조건 충족")])
        self.assertEqual(tracker.update(["라이다 수신 지연"], True, 1), [])
        self.assertEqual(tracker.update(["라이다 수신 지연"], True, 4), [("warn", "정지: 라이다 수신 지연")])
        self.assertEqual(tracker.update(["라이다 수신 지연"], True, 5), [])
        self.assertEqual(tracker.update([], True, 6), [])
        self.assertEqual(tracker.update([], True, 9), [("ok", "출발 조건 충족 (해소: 라이다 수신 지연)")])

    def test_flicker_of_a_reported_stop_stays_one_stop(self):
        tracker = WEB.BlockTracker()
        tracker.update(["a"], True, 0)
        tracker.update(["a"], True, 3)
        for now, reasons in ((4, []), (5, ["a"]), (6, []), (7, ["a"])):
            self.assertEqual(tracker.update(reasons, True, now), [])

    def test_short_stops_on_patrol_become_one_summary(self):
        tracker = WEB.BlockTracker()
        tracker.update([], True, 0)
        for now in range(1, 30, 2):
            self.assertEqual(tracker.update(["a"], True, now), [])
            self.assertEqual(tracker.update([], True, now + 1), [] if now + 1 < 30 else
                             [("warn", "최근 30초간 짧은 정지: a 15회")])
        self.assertFalse(tracker.short_stops)

    def test_summary_is_sent_when_patrol_ends_and_idle_blips_are_ignored(self):
        tracker = WEB.BlockTracker()
        tracker.update([], True, 0)
        tracker.update(["a"], True, 1)
        tracker.update([], True, 2)
        self.assertEqual(tracker.update([], False, 10), [("warn", "최근 10초간 짧은 정지: a 1회")])
        tracker.update(["a"], False, 11)
        self.assertEqual(tracker.update([], False, 12), [])
        self.assertEqual(tracker.update([], False, 100), [])

    def test_lasting_stop_is_reported_while_not_patrolling(self):
        tracker = WEB.BlockTracker()
        tracker.update(["a"], False, 0)
        self.assertEqual(tracker.update(["a"], False, 3), [("warn", "정지: a")])
        tracker.update([], False, 4)
        self.assertEqual(tracker.update([], False, 7), [("info", "해소: a")])

    def test_detail_changes_are_one_condition(self):
        tracker = WEB.BlockTracker()
        tracker.update(["장애물 안전거리 이내 (0.25m)", "자세 판정 수신 불량: no_frames"], True, 0)
        self.assertEqual(tracker.update(["장애물 안전거리 이내 (0.21m)", "자세 판정 수신 불량: inference_stale"], True, 1), [])
        self.assertEqual(len(tracker.conditions), 2)


class WaypointTrackerTests(unittest.TestCase):
    def test_each_leg_is_logged_once(self):
        tracker = WEB.WaypointTracker()
        self.assertEqual(tracker.update(0, 3, False, False), [])
        self.assertEqual(tracker.update(0, 3, True, False), [("info", "1/3번 지점으로 이동")])
        self.assertEqual(tracker.update(0, 3, True, False), [])
        self.assertEqual(tracker.update(0, 3, True, True), [("ok", "1/3번 지점 도착")])
        self.assertEqual(tracker.update(0, 3, True, True), [])
        self.assertEqual(tracker.update(1, 3, True, False), [("info", "2/3번 지점으로 이동")])

    def test_stopping_and_restarting_repeats_the_destination(self):
        tracker = WEB.WaypointTracker()
        tracker.update(1, 3, True, False)
        self.assertEqual(tracker.update(1, 3, False, False), [])
        self.assertEqual(tracker.update(1, 3, True, False), [("info", "2/3번 지점으로 이동")])

    def test_no_waypoints_is_silent(self):
        self.assertEqual(WEB.WaypointTracker().update(0, 0, True, False), [])


class WebLogTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.sent = []
        self.online = True

    def post(self, body):
        if self.online:
            self.sent.append(body)
        return self.online

    def log(self, **kwargs):
        return WEB.WebLog("http://s/", "patrol", post=self.post, clock=lambda: self.now,
                          wall=lambda: self.now, start=False, **kwargs)

    def test_repeats_collapse_into_a_count(self):
        log = self.log()
        for _ in range(4):
            log.add("warn", "drive", "정지: x")
        log.pump()
        self.assertEqual([b["message"] for b in self.sent], ["정지: x"])
        self.now += 31
        log.pump()
        self.assertEqual(self.sent[-1]["message"], "정지: x (최근 30초간 3회 더)")

    def test_collapse_can_be_turned_off(self):
        log = self.log()
        for _ in range(2):
            log.add("warn", "drive", "정지: x", collapse=False)
        log.pump()
        self.assertEqual([b["message"] for b in self.sent], ["정지: x", "정지: x"])

    def test_keeps_logs_while_server_is_down_and_marks_their_time(self):
        log = self.log()
        self.online = False
        log.add("error", "drive", "정지: 웹 서버 연결 대기/끊김")
        self.assertFalse(log.pump())
        self.now += 60
        self.online = True
        self.assertTrue(log.pump())
        self.assertRegex(self.sent[0]["message"], r"끊김 \(\d\d:\d\d:\d\d 발생\)$")

    def test_queue_is_bounded(self):
        log = self.log()
        self.online = False
        for i in range(500):
            log.add("info", "drive", str(i))
        self.assertEqual(len(log.queue), 200)

    def test_unreachable_server_never_raises(self):
        log = WEB.WebLog("http://127.0.0.1:9", "patrol", start=False)
        with patch.object(WEB.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertFalse(log._post(dict(message="x")))

    def test_delivery_exception_keeps_the_log_for_a_retry(self):
        log = self.log()
        log.add("warn", "drive", "x")
        log.post = lambda body: (_ for _ in ()).throw(OSError("offline"))
        self.assertFalse(log.pump())
        self.assertEqual(len(log.queue), 1)
        log.post = self.post
        self.assertTrue(log.pump())
        self.assertEqual([body["message"] for body in self.sent], ["x"])

    def test_shutdown_is_bounded_and_never_starts_another_delivery(self):
        entered, release = threading.Event(), threading.Event()
        sent = []

        def slow_post(body):
            sent.append(body)
            entered.set()
            release.wait(3)
            return True

        log = self.log()
        log.post = slow_post
        log.add("info", "drive", "first")
        log.add("info", "drive", "second")
        log.thread = threading.Thread(target=log._run, daemon=True)
        log.thread.start()
        try:
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            log.stop(timeout=0.03)
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertTrue(log.thread.is_alive())
            self.assertEqual(len(sent), 1)
        finally:
            release.set()
            log.thread.join(timeout=1)
        self.assertFalse(log.thread.is_alive())
        self.assertEqual(len(sent), 1)
        log.add("error", "drive", "late")
        self.assertEqual(len(log.queue), 1)
        self.assertTrue(log.pump())
        self.assertEqual(len(sent), 1)


class ControllerLogTests(unittest.TestCase):
    def test_failed_log_delivery_preserves_motion_event_and_voice(self):
        import test_patrol as patrol
        node = patrol.fake_node()
        self.addCleanup(shutil.rmtree, node.journal.parent)
        node.web_log = WEB.WebLog("http://server.invalid", "patrol", start=False,
                                  post=lambda body: False)
        node.block_tracker = WEB.BlockTracker()
        node.waypoint_tracker = WEB.WaypointTracker()
        node.log_started = time.monotonic() - 5
        node.last_logged_reason = node.last_block_report = None
        node.incident = dict(event_id=42, result="confirmed")
        node.voice = patrol.voice_module.VoiceLink()
        node.voice.start(100)
        original_voice = json.dumps(node.voice.request, sort_keys=True)
        original_event = dict(node.incident)
        original_goal = node.goal_pose
        states, waypoints = [], []
        node.state_pub = SimpleNamespace(publish=lambda message: states.append(message.data))
        node.waypoint_pub = SimpleNamespace(publish=lambda message: waypoints.append(message.data))
        node.http = lambda *a, **k: self.fail("log delivery must use its own transport")
        with patch.dict(patrol.ns, classify_reason=WEB.classify_reason,
                        classify_rosout=WEB.classify_rosout,
                        String=lambda **kw: SimpleNamespace(**kw),
                        Int32=lambda **kw: SimpleNamespace(**kw)):
            node.publish_state()
            node.on_rosout(SimpleNamespace(name="tb3_arduino_bridge", msg="Serial connection lost: test"))
        self.assertFalse(node.web_log.pump())
        self.assertEqual(states, ["patrol"])
        self.assertEqual(waypoints, [1])
        self.assertEqual(node.logic.phase, "patrol")
        self.assertTrue(node.drive_enabled)
        self.assertEqual(node.goal_pose, original_goal)
        self.assertEqual(node.incident, original_event)
        self.assertEqual(json.dumps(node.voice.request, sort_keys=True), original_voice)
        self.assertTrue(any(item["category"] == "output" for item in node.web_log.queue))
