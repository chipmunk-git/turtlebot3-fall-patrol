"""Exercise the actual relay loop without ROS2, HTTP or serial hardware."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as O
import unittest
from unittest.mock import patch
from urllib.error import URLError


class RelayTests(unittest.TestCase):
    def run_relay(self, states, times=None, raw_responses=False, alarms=None, raw_alarms=False,
                  interrupt_sleep=None, log_error=None):
        outputs = {}
        publishers = {}
        cleanup = []
        self.relay_outputs = outputs
        self.relay_cleanup = cleanup
        self.relay_events = []
        self.relay_logs = []
        events = self.relay_events
        runtime = O(alive=False, sigint_shutdown=True, destroyed=False)
        class Publisher:
            def publish(self, message):
                if not runtime.alive or runtime.destroyed:
                    raise RuntimeError('Cannot publish after ROS2 shutdown or node destruction')
                outputs[self.topic].append(message.data)
                events.append(('publish', self.topic, message.data))
            def get_subscription_count(self):
                return 1
        class Node:
            def __init__(self, name):
                pass
            def create_publisher(self, kind, topic, depth):
                publisher = Publisher();publisher.topic = topic
                outputs[topic] = [];publishers[topic] = publisher
                return publisher
            def destroy_node(self):
                runtime.destroyed = True
                events.append(('destroy_node',))
                cleanup.append('node')
        ros, rosnode = ModuleType('rclpy'), ModuleType('rclpy.node')
        signals = ModuleType('rclpy.signals')
        signals.SignalHandlerOptions = O(NO=0)
        def init(*, signal_handler_options=None):
            runtime.alive = True
            runtime.sigint_shutdown = signal_handler_options != signals.SignalHandlerOptions.NO
        def shutdown():
            runtime.alive = False
            events.append(('shutdown',))
            cleanup.append('ros')
        ros.init = init
        ros.shutdown = shutdown
        rosnode.Node = Node
        messages = ModuleType('std_msgs.msg')
        messages.String = messages.Bool = lambda **values: O(**values)
        modules = {'rclpy': ros, 'rclpy.node': rosnode, 'rclpy.signals': signals,
                   'std_msgs': ModuleType('std_msgs'), 'std_msgs.msg': messages}
        spec = importlib.util.spec_from_file_location('_status_relay_test',
                    Path(__file__).with_name('status_to_arduino.py'))
        relay = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(relay)
        responses = iter(states)
        alarm_responses = iter(alarms) if alarms is not None else None
        clock = iter(times if times is not None else [float(i) for i in range(len(states))])
        def interrupt():
            events.append(('signal', 'SIGINT'))
            # Model Humble's default signal handler invalidating the context.
            if runtime.sigint_shutdown:
                runtime.alive = False
            raise KeyboardInterrupt
        interrupted = False
        def sleep(seconds):
            nonlocal interrupted
            if seconds == interrupt_sleep and not interrupted:
                interrupted = True
                interrupt()
        def urlopen(url, timeout):
            if not isinstance(url, str):
                self.relay_logs.append(json.loads(url.data))
                if log_error is not None:
                    raise log_error
                return io.StringIO('')
            if url.endswith('/events/alarms'):
                events.append(('request', 'alarms'))
                value = next(alarm_responses) if alarm_responses is not None else []
                if isinstance(value, Exception):
                    raise value
                return io.StringIO(value if raw_alarms else json.dumps({'active_event_ids': value}))
            try:
                value = next(responses)
            except StopIteration:
                interrupt()
            events.append(('request', 'status'))
            if isinstance(value, Exception):
                raise value
            return io.StringIO(value if raw_responses else json.dumps({'state': value}))
        relay.urlopen = urlopen
        relay.time = O(sleep=sleep, monotonic=lambda: next(clock))
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['status_to_arduino.py']), contextlib.redirect_stdout(stdout):
            relay.main()
        return outputs, cleanup, stdout.getvalue(), relay

    def test_four_states_publish_blue_green_yellow_red_and_preserve_lcd(self):
        outputs, cleanup, text, relay = self.run_relay(['idle', 'patrol', 'observe', 'alert'])
        self.assertEqual(set(relay.STATES), {'idle', 'patrol', 'observe', 'alert'})
        self.assertEqual(outputs['/tb3/rgb_led'][:-2],
                         ['0,0,255']*2 + ['0,255,0']*2 + ['255,255,0']*2 + ['255,0,0']*2)
        self.assertEqual(outputs['/tb3/lcd_text'][:-2],
                         ['IDLE|STANDBY']*2 + ['MONITORING|PATROL ACTIVE']*2
                         + ['OBSERVING|PLEASE WAIT']*2 + ['FALL SUSPECTED|STAFF CHECK']*2)
        self.assertTrue(all(value is False for value in outputs['/tb3/buzzer']))

    def test_legacy_states_use_observation_outputs_without_extra_transition_preview(self):
        outputs, _, text, _ = self.run_relay(['suspect', 'approach', 'observe'], [0, 0.2, 0.4])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,255,0']*2)
        self.assertEqual(outputs['/tb3/lcd_text'][:-2], ['OBSERVING|PLEASE WAIT']*2)
        self.assertTrue(all(value is False for value in outputs['/tb3/buzzer']))
        self.assertEqual(text.count('[가상 스피커]'), 1)

    def test_unchanged_state_refreshes_rgb_lcd_and_power(self):
        outputs, _, text, _ = self.run_relay(['idle']*3, [0, 0.5, 1.1])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['0,0,255']*4)
        self.assertEqual(outputs['/tb3/lcd_text'][:-2], ['IDLE|STANDBY']*4)
        self.assertEqual(outputs['/tb3/lcd_power'], [True]*6 + [False]*2)
        self.assertTrue(all(value is False for value in outputs['/tb3/buzzer']))
        self.assertEqual(text.count('[상태]'), 1)

    def test_state_change_is_sent_before_refresh_deadline(self):
        outputs, _, _, _ = self.run_relay(['idle', 'patrol'], [0, 0.2])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['0,0,255']*2 + ['0,255,0']*2)

    def test_server_failure_then_recovery_refreshes_same_state(self):
        outputs, _, text, _ = self.run_relay(['idle', URLError('offline'), 'idle'], [0, 1.1])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['0,0,255']*4)
        self.assertIn('[서버 통신 실패]', text)
        self.assertEqual([log['level'] for log in self.relay_logs], ['error'])
        self.assertIn('서버 통신 실패 후 복구', self.relay_logs[0]['message'])

    def test_log_delivery_failure_does_not_stop_the_relay(self):
        outputs, _, _, relay = self.run_relay(['idle'], [0])
        relay.urlopen = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
        self.assertFalse(relay.post_log('http://s', 'error', 'x'))

    def test_log_failure_keeps_display_updates_and_buzzer_off(self):
        outputs, _, _, _ = self.run_relay(['idle', 'patrol'], [0, 1],
                                         alarms=[URLError('alarm offline'), []],
                                         log_error=URLError('log offline'))
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['0,0,255']*2 + ['0,255,0']*2)
        self.assertTrue(all(value is False for value in outputs['/tb3/buzzer']))

    def test_interrupt_during_log_delivery_still_turns_everything_off(self):
        outputs, cleanup, _, _ = self.run_relay(['idle'], [0],
                                               alarms=[URLError('alarm offline')],
                                               log_error=KeyboardInterrupt())
        self.assertEqual(outputs['/tb3/buzzer'][-2:], [False, False])
        self.assertEqual(outputs['/tb3/rgb_led'][-2:], ['0,0,0', '0,0,0'])
        self.assertEqual(outputs['/tb3/lcd_text'][-2:], ['', ''])
        self.assertEqual(outputs['/tb3/lcd_power'][-2:], [False, False])
        self.assertEqual(cleanup, ['node', 'ros'])

    def test_unknown_state_is_not_published_as_idle(self):
        outputs, _, text, _ = self.run_relay(['unknown'], [0])
        self.assertEqual(outputs['/tb3/rgb_led'], ['0,0,0']*2)
        self.assertIn('알 수 없는 상태', text)

    def test_shutdown_turns_off_outputs_and_cleans_up(self):
        for delay in (None, 2.0, 0.1):
            with self.subTest(interrupt_sleep=delay):
                outputs, cleanup, _, _ = self.run_relay(['alert'], alarms=[[9]],
                                                       interrupt_sleep=delay)
                if delay is None:
                    self.assertIn(True, outputs['/tb3/buzzer'])
                self.assertEqual(self.relay_events[-10:],
                                 [('publish', '/tb3/buzzer', False)]*2
                                 + [('publish', '/tb3/rgb_led', '0,0,0')]*2
                                 + [('publish', '/tb3/lcd_text', '')]*2
                                 + [('publish', '/tb3/lcd_power', False)]*2
                                 + [('destroy_node',), ('shutdown',)])
                self.assertEqual(cleanup, ['node', 'ros'])

    def test_pending_popup_alarm_is_independent_of_robot_state(self):
        for state in ('idle', 'patrol', 'observe', 'alert', 'suspect', 'approach', 'unknown'):
            with self.subTest(state=state):
                outputs, _, _, _ = self.run_relay([state], [0], alarms=[[9]])
                self.assertEqual(outputs['/tb3/buzzer'], [False]*2 + [True]*2 + [False]*2)

    def test_pending_popup_alarm_refreshes_for_late_or_restarted_bridge(self):
        outputs, _, text, _ = self.run_relay(['alert']*3, [0, 0.5, 1.1], alarms=[[9]]*3)
        self.assertEqual(outputs['/tb3/buzzer'], [False]*2 + [True]*4 + [False]*2)
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,0,0']*4)
        self.assertEqual(text.count('[상태]'), 1)
        self.assertEqual(text.count('[가상 스피커]'), 1)

    def test_server_failure_stops_buzzer_before_next_request(self):
        for error in (URLError('offline'), TimeoutError('timeout'), OSError('offline'),
                      ValueError('invalid response'), KeyError('state')):
            with self.subTest(error=type(error).__name__):
                outputs, _, _, _ = self.run_relay(['alert', error, 'idle'], [0, 0.2], alarms=[[9], []])
                requests = [i for i, event in enumerate(self.relay_events) if event == ('request', 'status')]
                self.assertEqual(self.relay_events[requests[1]+1:requests[2]],
                                 [('publish', '/tb3/buzzer', False)]*2)
                self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,0,0']*2 + ['0,0,255']*2)

    def test_recovered_event_alarm_enables_buzzer_before_rgb_refresh_deadline(self):
        outputs, _, text, _ = self.run_relay(['alert', URLError('offline'), 'alert'], [0, 0.2], alarms=[[9], [9]])
        self.assertEqual(outputs['/tb3/buzzer'],
                         [False]*2 + [True]*2 + [False]*2 + [True]*2 + [False]*2)
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,0,0']*2)
        self.assertEqual(text.count('[상태]'), 1)

    def test_repeated_failures_keep_buzzer_off_without_repeating_warning(self):
        outputs, _, text, _ = self.run_relay(['alert', URLError('offline'), URLError('offline')], [0], alarms=[[9]])
        self.assertEqual(outputs['/tb3/buzzer'], [False]*2 + [True]*2 + [False]*6)
        self.assertEqual(text.count('[서버 통신 실패]'), 1)
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,0,0']*2)

    def test_unknown_robot_state_does_not_hide_pending_event_alarm(self):
        outputs, _, text, _ = self.run_relay(['alert', 'unknown', 'unknown'], [0, 0.2, 0.4], alarms=[[9]]*3)
        self.assertEqual(outputs['/tb3/buzzer'], [False]*2 + [True]*2 + [False]*2)
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,0,0']*2)
        self.assertEqual(outputs['/tb3/lcd_text'][:-2], ['FALL SUSPECTED|STAFF CHECK']*2)
        self.assertEqual(text.count('알 수 없는 상태'), 1)

    def test_invalid_status_response_stops_buzzer_and_polling_recovers(self):
        for body in ('not json', '{}', '[]', 'null', '"alert"', '{"state": null}',
                     '{"state": []}', '{"state": {}}', '{"state": 1}', '{"state": true}'):
            with self.subTest(body=body):
                outputs, cleanup, text, _ = self.run_relay(
                    ['{"state": "alert"}', body, '{"state": "patrol"}'],
                    [0, 0.2], raw_responses=True, alarms=[[9], [9]])
                self.assertEqual(outputs['/tb3/buzzer'],
                                 [False]*2 + [True]*2 + [False]*2 + [True]*2 + [False]*2)
                self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,0,0']*2 + ['0,255,0']*2)
                self.assertEqual(text.count('[서버 통신 실패]'), 1)
                self.assertEqual(cleanup, ['node', 'ros'])

    def test_unexpected_error_still_turns_off_buzzer_and_cleans_up(self):
        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            self.run_relay(['alert', RuntimeError('unexpected')], [0], alarms=[[9]])
        self.assertEqual(self.relay_outputs['/tb3/buzzer'], [False]*2 + [True]*2 + [False]*2)
        self.assertEqual(self.relay_cleanup, ['node', 'ros'])

    def test_startup_without_server_never_enables_buzzer(self):
        outputs, cleanup, _, _ = self.run_relay([URLError('offline')], [])
        self.assertEqual(outputs['/tb3/buzzer'], [False]*6)
        self.assertEqual(outputs['/tb3/rgb_led'], ['0,0,0']*2)
        self.assertEqual(cleanup, ['node', 'ros'])

    def test_robot_alert_without_presented_pending_events_never_enables_buzzer(self):
        outputs, _, _, _ = self.run_relay(['alert']*2, [0, 0.2], alarms=[[], []])
        self.assertTrue(all(value is False for value in outputs['/tb3/buzzer']))

    def test_alarm_starts_only_after_server_reports_popup_presentation(self):
        outputs, _, _, _ = self.run_relay(['observe']*2, [0, 0.2], alarms=[[], [9]])
        requests = [i for i, event in enumerate(self.relay_events) if event == ('request', 'alarms')]
        self.assertNotIn(('publish', '/tb3/buzzer', True), self.relay_events[:requests[1]])
        self.assertIn(True, outputs['/tb3/buzzer'])

    def test_resolved_or_muted_alarm_stops_even_while_robot_remains_alert(self):
        outputs, _, _, _ = self.run_relay(['alert']*2, [0, 0.2], alarms=[[9], []])
        self.assertEqual(outputs['/tb3/buzzer'], [False]*2 + [True]*2 + [False]*4)

    def test_one_muted_event_does_not_stop_another_and_new_events_can_ring(self):
        outputs, _, _, _ = self.run_relay(['idle']*4, [0, 0.2, 0.4, 0.6],
                                        alarms=[[7, 8], [8], [], [9]])
        self.assertEqual(outputs['/tb3/buzzer'],
                         [False]*2 + [True]*2 + [False]*2 + [True]*2 + [False]*2)

    def test_alarm_lookup_failure_stops_buzzer_but_rgb_and_lcd_keep_updating(self):
        outputs, _, text, _ = self.run_relay(['alert', 'patrol', 'observe'], [0, 0.2, 0.4],
                                           alarms=[[9], URLError('offline'), [9]])
        self.assertEqual(outputs['/tb3/buzzer'],
                         [False]*2 + [True]*2 + [False]*2 + [True]*2 + [False]*2)
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,0,0']*2 + ['0,255,0']*2 + ['255,255,0']*2)
        self.assertEqual(outputs['/tb3/lcd_text'][:-2], ['FALL SUSPECTED|STAFF CHECK']*2
                         + ['MONITORING|PATROL ACTIVE']*2 + ['OBSERVING|PLEASE WAIT']*2)
        self.assertIn('[사건 경보 조회 실패]', text)
        self.assertIn('사건 경보 조회 실패', self.relay_logs[0]['message'])

    def test_malformed_alarm_response_stops_buzzer_and_polling_recovers(self):
        for body in ('not json', '{}', '[]', 'null', '{"active_event_ids": true}',
                     '{"active_event_ids": [true]}', '{"active_event_ids": [0]}',
                     '{"active_event_ids": ["9"]}', '{"active_event_ids": [-1]}'):
            with self.subTest(body=body):
                outputs, _, text, _ = self.run_relay(['idle']*3, [0, 0.2, 0.4],
                    alarms=['{"active_event_ids": [9]}', body, '{"active_event_ids": [9]}'],
                    raw_alarms=True)
                self.assertEqual(outputs['/tb3/buzzer'],
                                 [False]*2 + [True]*2 + [False]*2 + [True]*2 + [False]*2)
                self.assertEqual(text.count('[사건 경보 조회 실패]'), 1)


if __name__ == '__main__':
    unittest.main()
