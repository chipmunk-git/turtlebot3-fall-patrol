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
    def run_relay(self, states, times=None):
        outputs = {}
        publishers = {}
        cleanup = []
        class Publisher:
            def publish(self, message):
                outputs[self.topic].append(message.data)
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
                cleanup.append('node')
        ros, rosnode = ModuleType('rclpy'), ModuleType('rclpy.node')
        ros.init = lambda: None
        ros.shutdown = lambda: cleanup.append('ros')
        rosnode.Node = Node
        messages = ModuleType('std_msgs.msg')
        messages.String = messages.Bool = lambda **values: O(**values)
        modules = {'rclpy': ros, 'rclpy.node': rosnode,
                   'std_msgs': ModuleType('std_msgs'), 'std_msgs.msg': messages}
        spec = importlib.util.spec_from_file_location('_status_relay_test',
                    Path(__file__).with_name('status_to_arduino.py'))
        relay = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(relay)
        responses = iter(states)
        clock = iter(times if times is not None else [float(i) for i in range(len(states))])
        def urlopen(url, timeout):
            try:
                value = next(responses)
            except StopIteration:
                raise KeyboardInterrupt
            if isinstance(value, Exception):
                raise value
            return io.StringIO(json.dumps({'state': value}))
        relay.urlopen = urlopen
        relay.time = O(sleep=lambda seconds: None, monotonic=lambda: next(clock))
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
        self.assertNotIn('/tb3/buzzer', outputs)

    def test_legacy_states_use_observation_outputs_without_extra_transition_preview(self):
        outputs, _, text, _ = self.run_relay(['suspect', 'approach', 'observe'], [0, 0.2, 0.4])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['255,255,0']*2)
        self.assertEqual(outputs['/tb3/lcd_text'][:-2], ['OBSERVING|PLEASE WAIT']*2)
        self.assertEqual(text.count('[가상 스피커]'), 1)

    def test_unchanged_state_refreshes_rgb_lcd_and_power(self):
        outputs, _, text, _ = self.run_relay(['idle']*3, [0, 0.5, 1.1])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['0,0,255']*4)
        self.assertEqual(outputs['/tb3/lcd_text'][:-2], ['IDLE|STANDBY']*4)
        self.assertEqual(outputs['/tb3/lcd_power'], [True]*6 + [False]*2)
        self.assertEqual(text.count('[상태]'), 1)

    def test_state_change_is_sent_before_refresh_deadline(self):
        outputs, _, _, _ = self.run_relay(['idle', 'patrol'], [0, 0.2])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['0,0,255']*2 + ['0,255,0']*2)

    def test_server_failure_then_recovery_refreshes_same_state(self):
        outputs, _, text, _ = self.run_relay(['idle', URLError('offline'), 'idle'], [0, 1.1])
        self.assertEqual(outputs['/tb3/rgb_led'][:-2], ['0,0,255']*4)
        self.assertIn('[서버 통신 실패]', text)

    def test_unknown_state_is_not_published_as_idle(self):
        outputs, _, text, _ = self.run_relay(['unknown'], [0])
        self.assertEqual(outputs['/tb3/rgb_led'], ['0,0,0']*2)
        self.assertIn('알 수 없는 상태', text)

    def test_shutdown_turns_off_outputs_and_cleans_up(self):
        outputs, cleanup, _, _ = self.run_relay(['idle'])
        self.assertEqual(outputs['/tb3/rgb_led'][-2:], ['0,0,0']*2)
        self.assertEqual(outputs['/tb3/lcd_text'][-2:], ['']*2)
        self.assertEqual(outputs['/tb3/lcd_power'][-2:], [False]*2)
        self.assertEqual(cleanup, ['node', 'ros'])


if __name__ == '__main__':
    unittest.main()
