"""Mock-only checks: no running server, robot, or persistent database."""

import io
import json
import math
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fake_robot import FakeRobot, MapView, SCENARIOS
from scripts.patrol import Patrol
from scripts.png_utils import encode_png


class FakeRobotTests(unittest.TestCase):
    def setUp(self):
        patrol = Patrol([(0, 0), (1, 0)], [(0.5, 0)], 1.0, 0.35)
        self.robot = FakeRobot("http://localhost:8000", patrol, 0.3, 5, random.Random(0))
        self.robot.put_status = Mock()
        self.robot.look_around = Mock()

    def test_stop_and_failed_lookup_hold_position_until_explicit_run(self):
        self.robot.request = Mock(side_effect=[{"drive": "stop"}, None,
                                               {"drive": "run"}, {"drive": "run"}])
        positions = []
        with patch("scripts.fake_robot.time.sleep", side_effect=lambda _: positions.append(self.robot.x)):
            self.robot.drive_to((0.06, 0), "patrol")
        self.assertEqual(positions[:2], [0, 0])
        self.assertAlmostEqual(self.robot.x, 0.06)
        self.assertEqual([call.args[0] for call in self.robot.put_status.call_args_list],
                         ["idle", "idle", "patrol"])

    def test_repeated_lookup_failure_never_moves(self):
        self.robot.request = Mock(return_value=None)
        with patch("scripts.fake_robot.time.sleep", side_effect=[None, None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                self.robot.drive_to((1, 0), "patrol")
        self.assertEqual((self.robot.x, self.robot.y), (0, 0))
        self.assertEqual(self.robot.request.call_count, 3)
        self.assertTrue(all(call.args == ("idle",) for call in self.robot.put_status.call_args_list))

    def test_malformed_or_unknown_commands_wait_instead_of_allowing_motion(self):
        for response in ({}, [], {"drive": "fast"}, {"drive": None}, "run"):
            with self.subTest(response=response):
                self.robot.request = Mock(side_effect=[response, {"drive": "run"}])
                with patch("scripts.fake_robot.time.sleep") as sleep:
                    self.robot.wait_for_run()
                sleep.assert_called_once_with(0.5)

    def test_network_and_invalid_json_errors_wait_then_recover(self):
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"drive":"run"}'))
        response.__exit__ = Mock(return_value=False)
        for failure in (urllib.error.URLError("offline"), TimeoutError(),
                        OSError("connection reset"), json.JSONDecodeError("invalid", "", 0)):
            with self.subTest(failure=type(failure).__name__):
                with patch("scripts.fake_robot.urllib.request.urlopen", side_effect=[failure, response]), \
                     patch("scripts.fake_robot.time.sleep") as sleep:
                    # BytesIO must be fresh for each successful response.
                    response.__enter__.return_value = io.BytesIO(b'{"drive":"run"}')
                    self.robot.wait_for_run()
                sleep.assert_called_once_with(0.5)

    def test_failed_idle_report_does_not_release_wait(self):
        self.robot.request = Mock(side_effect=[None, {"drive": "run"}])
        self.robot.put_status.side_effect = OSError("offline")
        with patch("scripts.fake_robot.time.sleep") as sleep:
            self.robot.wait_for_run()
        sleep.assert_called_once_with(0.5)
        self.assertEqual((self.robot.x, self.robot.y), (0, 0))

    def test_scenario_cycle_and_random_mode_use_only_supported_outcomes(self):
        self.assertEqual([self.robot.next_scenario() for _ in range(6)], SCENARIOS * 2)
        self.robot.random_results = True
        self.assertTrue(set(self.robot.next_scenario() for _ in range(30)).issubset(SCENARIOS))

    def prepare_person(self, scenario):
        self.robot.request = Mock(return_value={"ignored": False})
        self.robot.drive_to = Mock()
        self.robot.hold = Mock()
        self.robot.next_scenario = Mock(return_value=scenario)
        self.robot.post_multipart = Mock(return_value={"id": 42})
        self.robot.wait_for_web = Mock()

    def test_normal_recovery_creates_no_event(self):
        self.prepare_person("normal")
        with patch("scripts.fake_robot.make_snapshot", return_value=b"image"):
            self.robot.handle_person((0.5, 0))
        self.robot.post_multipart.assert_not_called()
        self.robot.wait_for_web.assert_not_called()

    def test_alert_scenarios_create_one_event_with_photos_and_wait_for_web(self):
        for scenario in ("confirmed", "uncertain"):
            with self.subTest(scenario=scenario):
                self.prepare_person(scenario)
                with patch("scripts.fake_robot.make_snapshot", return_value=b"image"):
                    self.robot.handle_person((0.5, 0))
                calls = self.robot.post_multipart.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0].args[0], "/events")
                self.assertEqual(calls[0].args[1]["status"], scenario)
                self.assertEqual(set(calls[0].args[2]), {"detected_snapshot", "observing_snapshot"})
                self.assertEqual(calls[1].args[0], "/events/42/snapshots")
                self.robot.wait_for_web.assert_called_once_with(42)

    def test_web_acknowledgement_and_dismissal_release_wait(self):
        for status in ("acknowledged", "resolved", "false_alarm"):
            with self.subTest(status=status):
                self.robot.ack_wait = 0
                self.robot.request = Mock(side_effect=[None, {"status": "confirmed"}, {"status": status}])
                self.robot.hold = Mock()
                self.robot.wait_for_web(42)
                self.assertEqual(self.robot.hold.call_count, 2)
                self.assertTrue(all(call.args == ("GET", "/events/42")
                                    for call in self.robot.request.call_args_list))

    def test_default_web_wait_expires_without_editing_event_status(self):
        self.robot.request = Mock(return_value={"status": "confirmed"})
        self.robot.hold = Mock()
        with patch("scripts.fake_robot.time.monotonic", side_effect=[0, 0, 31]):
            self.robot.wait_for_web(42)
        self.robot.hold.assert_called_once_with("alert", 1.0)
        self.robot.request.assert_called_once_with("GET", "/events/42")

    def test_rotated_map_distinguishes_free_space_and_walls(self):
        image = encode_png(2, 2, bytes([0, 0, 254, 0]), 1)
        view = MapView({"resolution": 1, "origin_x": 10, "origin_y": 20,
                        "origin_yaw": math.pi / 2}, image)
        self.assertTrue(view.is_free(9.5, 20.5))
        self.assertFalse(view.is_free(9.5, 21.5))


if __name__ == "__main__":
    unittest.main()
