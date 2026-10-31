"""Real patrol methods with mocked ROS, HTTP and sensors; no hardware or server."""
import json
import shutil
import time
from types import SimpleNamespace as O
import unittest
from unittest.mock import patch
import urllib.request

from test_patrol import fake_node, ns, voice_module

ns['urllib'] = urllib


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.n = fake_node()
        self.n.clear_observation_image()
        self.addCleanup(shutil.rmtree, self.n.journal.parent, True)
        self.command = 'run'
        self.calls = []
        self.n.http = self.http

    def http(self, method, path, data=None, *args):
        self.calls.append((method, path, data))
        if path == '/control':
            if method == 'PUT':
                self.command = data['drive']
            return {'drive': self.command}
        if path.startswith('/events/'):
            return {'status': 'confirmed', 'acknowledged_at': None, 'note': '관리자 메모'}
        return {'status': 'ok'}

    def ready(self):
        self.n.pause_drive('ready')

    def test_rviz_waypoints_prepare_without_automatic_driving(self):
        n = self.n
        n.drive_enabled = False
        n.logic.phase = 'setup'
        n.waypoints = []
        n.goal_pose = None

        class Grid:
            shape = (10, 10)
            def __getitem__(self, key):
                return False

        n.base_blocked = Grid()
        with patch.dict(ns, world_to_grid=lambda x, y, *args: (int(y), int(x))):
            for x, y in ((0, 0), (1, 0), (1, 1)):
                n.goal_callback(O(header=O(frame_id='map'), pose=O(position=O(x=x, y=y))))
        self.assertEqual(n.logic.phase, 'ready')
        self.assertFalse(n.drive_enabled)
        self.assertIsNone(n.goal_pose)
        self.assertFalse(n.control_initialized)
        # An early click remains on the server until the next poll.
        self.assertEqual(self.command, 'run')
        n.poll_drive_control()
        n.poll_drive_control()
        self.assertFalse(n.drive_enabled)
        self.command = 'run'
        n.poll_drive_control()
        self.assertTrue(n.drive_enabled)

    def test_startup_discards_saved_run_and_requires_a_new_click(self):
        self.ready()
        self.n.control_initialized = False
        self.n.poll_drive_control()
        self.assertEqual(self.command, 'stop')
        self.assertFalse(self.n.drive_enabled)
        self.n.poll_drive_control()
        self.assertFalse(self.n.drive_enabled)
        self.command = 'run'
        self.n.poll_drive_control()
        self.assertTrue(self.n.drive_enabled)
        self.assertEqual(self.n.logic.phase, 'patrol')
        self.assertTrue(self.n.need_plan)

    def test_run_before_waypoints_are_ready_is_not_replayed_later(self):
        self.ready()
        self.n.logic.phase = 'setup'
        self.n.waypoints = []
        self.n.poll_drive_control()
        self.assertFalse(self.n.control_initialized)
        self.n.waypoints = [(0, 0), (1, 0), (1, 1)]
        self.n.logic.phase = 'ready'
        self.n.poll_drive_control()
        self.n.poll_drive_control()
        self.assertFalse(self.n.drive_enabled)
        self.command = 'run'
        self.n.poll_drive_control()
        self.assertTrue(self.n.drive_enabled)

    def test_stop_preserves_destination_and_run_replans_from_current_pose(self):
        self.n.waypoint_index = 1
        self.n.path_xy = ['old path']
        self.n.apply_drive_command('stop')
        self.assertEqual(self.n.waypoint_index, 1)
        self.assertIsNone(self.n.goal_pose)
        self.assertFalse(self.n.drive_enabled)
        self.n.current_pose = (0.5, 0.5)
        self.assertTrue(self.n.apply_drive_command('run'))
        self.assertEqual(self.n.goal_pose, (1, 0))
        self.assertEqual(self.n.path_xy, [])
        self.assertTrue(self.n.need_plan)
        self.assertEqual(self.n.current_pose, (0.5, 0.5))

    def test_stop_while_waiting_advances_resume_target_only_once(self):
        self.n.wait_until = time.monotonic() + 2
        self.n.apply_drive_command('stop')
        self.n.apply_drive_command('stop')
        self.assertEqual(self.n.waypoint_index, 1)
        self.n.apply_drive_command('run')
        self.assertEqual(self.n.goal_pose, (1, 0))

    def test_control_failure_stops_but_status_reporting_continues(self):
        for failure_method in ('GET', 'PUT'):
            with self.subTest(method=failure_method):
                self.n.control_initialized = failure_method == 'GET'
                self.n.drive_enabled = True
                self.n.logic.phase = 'patrol'
                self.n.goal_pose = (1, 0)
                def http(method, path, data=None, *args):
                    if path == '/control' and method == failure_method:
                        raise OSError('control unavailable')
                    return self.http(method, path, data, *args)
                self.n.http = http
                self.calls.clear()
                self.n.web_cycle()
                self.assertFalse(self.n.drive_enabled)
                self.assertFalse(self.n.control_initialized)
                self.assertTrue(any(m == 'PUT' and p == '/status' for m, p, _ in self.calls))

    def test_control_failure_does_not_block_event_creation_or_same_event_voice_note(self):
        n = self.n
        n.logic.phase = 'alert'
        n.voice_enabled = True
        n.voice = voice_module.VoiceLink()
        n.voice.start(time.time())
        key = n.voice.request['request_id']
        n.incident = dict(token='observation', event_id=None, result='confirmed', x=1, y=2,
                          voice_request_id=key)
        n.on_voice_result(O(data=json.dumps(dict(request_id=key, status='ok',
            text='도와주세요', classification='도움 필요'))))
        created, notes = [], {'note': '관리자 메모'}
        def http(method, path, data=None, *args):
            self.calls.append((method, path, data))
            if path == '/control':
                raise OSError('control unavailable')
            if path == '/events/7':
                if method == 'PATCH':
                    notes.update(data)
                return dict(status='confirmed', acknowledged_at=None, **notes)
            return {'status': 'ok'}
        n.http = http
        n.post_incident = lambda incident: (created.append(incident['token']) or {'id': 7})
        n.web_cycle()
        n.web_cycle()
        self.assertEqual(created, ['observation'])
        self.assertEqual(n.incident['event_id'], 7)
        self.assertEqual(n.logic.phase, 'alert')
        self.assertTrue(notes['note'].startswith('관리자 메모'))
        self.assertEqual(notes['note'].count('[voice:' + key + ']'), 1)
        self.assertIn('도움 필요', notes['note'])
        self.assertTrue(all(set(data) == {'note'} for method, path, data in self.calls if method == 'PATCH'))

    def test_malformed_control_response_blocks_motion_without_aborting_reporting(self):
        for initialized in (True, False):
            replies = (None, [], 'run', {}, {'drive': 'unknown'})
            if not initialized:
                replies += ({'drive': 'run'},)
            for reply in replies:
                with self.subTest(initialized=initialized, reply=reply):
                    self.n.control_initialized = initialized
                    self.n.drive_enabled = True
                    self.n.logic.phase = 'patrol'
                    self.n.http = lambda method, path, data=None, *args: (
                        reply if path == '/control' else self.http(method, path, data, *args))
                    self.n.web_cycle()
                    self.assertFalse(self.n.drive_enabled)
                    self.assertFalse(self.n.control_initialized)

    def test_timeout_requires_fresh_run_after_connection_recovers(self):
        self.n.server_seen -= 4
        self.n.control_loop()
        self.assertFalse(self.n.drive_enabled)
        self.assertFalse(self.n.control_initialized)
        self.n.server_seen = time.monotonic()
        self.n.poll_drive_control()
        self.n.poll_drive_control()
        self.assertFalse(self.n.drive_enabled)
        self.command = 'run'
        self.n.poll_drive_control()
        self.assertTrue(self.n.drive_enabled)

    def test_http_network_failure_revokes_drive_authorization(self):
        with patch('urllib.request.urlopen', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                type(self.n).http(self.n, 'GET', '/health')
        self.assertFalse(self.n.drive_enabled)
        self.assertFalse(self.n.control_initialized)
        self.assertEqual(self.n.logic.phase, 'ready')

    def test_late_control_response_cannot_override_a_new_pause(self):
        self.ready()
        def http(method, path, data=None, *args):
            self.n.pause_drive('intervening alarm', reset_control=True)
            return {'drive': 'run'}
        self.n.http = http
        self.n.poll_drive_control()
        self.assertFalse(self.n.drive_enabled)
        self.assertFalse(self.n.control_initialized)

    def test_late_reset_response_cannot_arm_a_newer_alarm_generation(self):
        self.ready()
        self.n.control_initialized = False
        self.n.http = lambda *args: (self.n.pause_drive('new alarm', reset_control=True) or {'drive': 'stop'})
        self.n.poll_drive_control()
        self.assertFalse(self.n.control_initialized)

    def test_alert_revokes_run_without_cancelling_active_voice(self):
        self.n.voice_enabled = True
        self.n.voice = voice_module.VoiceLink()
        self.n.voice.start(time.time())
        key = self.n.voice.request['request_id']
        self.n.logic.phase = 'alert'
        self.n.logic.result = 'confirmed'
        self.n.control_loop()
        self.assertFalse(self.n.drive_enabled)
        self.assertEqual(self.n.incident['voice_request_id'], key)
        self.assertEqual(self.n.voice.request['request_id'], key)
        self.assertIsNone(self.n.voice.cancel)

    def test_resolved_without_legacy_ack_requires_fresh_run_after_sensor_clearance(self):
        self.n.logic.phase = 'alert'
        self.n.incident = dict(token='event', event_id=7, result='confirmed', note_sent=True)
        self.n.http = lambda method, path, data=None, *args: (
            {'status': 'resolved', 'acknowledged_at': None} if path == '/events/7'
            else self.http(method, path, data, *args))
        self.n.web_cycle()
        self.assertEqual(self.n.logic.phase, 'ack_wait')
        self.n.base_blocked_test = True
        self.n.control_loop()
        self.assertEqual(self.n.logic.phase, 'ack_wait')
        self.n.base_blocked_test = False
        self.n.control_loop()
        self.assertEqual(self.n.logic.phase, 'ready')
        self.assertFalse(self.n.control_initialized)
        self.n.poll_drive_control()
        self.n.poll_drive_control()
        self.assertFalse(self.n.drive_enabled)
        self.command = 'run'
        self.n.poll_drive_control()
        self.assertTrue(self.n.drive_enabled)

    def test_stop_during_observation_keeps_voice_and_does_not_restart_on_normal_recovery(self):
        self.n.voice_enabled = True
        self.n.voice = voice_module.VoiceLink()
        self.n.voice.start(time.time())
        key = self.n.voice.request['request_id']
        self.n.logic.phase = 'observe'
        self.n.apply_drive_command('stop')
        self.assertEqual(self.n.logic.phase, 'observe')
        self.assertEqual(self.n.voice.request['request_id'], key)
        self.n.logic.phase = 'patrol'  # Existing normal-posture recovery.
        self.n.control_loop()
        self.assertEqual(self.n.logic.phase, 'ready')
        self.assertFalse(self.n.drive_enabled)
        self.assertEqual(self.n.voice.cancel['request_id'], key)

    def test_run_does_not_bypass_existing_sensor_safety_conditions(self):
        for broken in ('lidar', 'camera', 'person', 'pose'):
            with self.subTest(broken=broken):
                self.ready()
                self.n.scan_time = self.n.pose_seen = time.monotonic()
                self.n.current_pose = (0, 0)
                self.n.estop_active = False
                if broken == 'lidar': self.n.scan_time -= 10
                if broken == 'camera': self.n.pose_seen -= 10
                if broken == 'person': self.n.estop_active = True
                if broken == 'pose': self.n.current_pose = None
                self.assertTrue(self.n.apply_drive_command('run'))
                self.n.publish_smooth(0.1, 0)
                self.assertEqual(self.n.published, [])

    def test_stop_prevents_a_late_nonzero_velocity(self):
        self.n.apply_drive_command('stop')
        self.n.publish_smooth(0.1, 0.2)
        self.assertEqual(self.n.published, [])


if __name__ == '__main__':
    unittest.main()
