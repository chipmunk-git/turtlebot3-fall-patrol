"""Offline behavior tests: no ROS2, NumPy, camera, model or motor is used."""

import contextlib
import copy
import importlib
import io
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch


NS = types.SimpleNamespace
EVENTS = []
ROS = NS(active=True)


def stamp(nanoseconds):
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    return NS(sec=seconds, nanosec=remainder)


def transform(nanoseconds=10_000_000_000):
    return NS(header=NS(stamp=stamp(nanoseconds)), transform=NS(
        translation=NS(x=0.0, y=0.0, z=0.0),
        rotation=NS(x=0.0, y=0.0, z=0.0, w=1.0)))


def pose_message(frame='map', nanoseconds=1):
    return NS(header=NS(frame_id=frame, stamp=stamp(nanoseconds)), pose=NS(pose=NS(
        position=NS(x=0.0, y=0.0), orientation=NS(x=0.0, y=0.0, z=0.0, w=1.0))))


class Twist:
    def __init__(self):
        self.linear = NS(x=0.0, y=0.0, z=0.0)
        self.angular = NS(x=0.0, y=0.0, z=0.0)


class Publisher:
    def __init__(self, topic):
        self.topic = topic
        self.messages = []

    def publish(self, message):
        self.messages.append(copy.deepcopy(message))
        if isinstance(message, Twist):
            EVENTS.append(('velocity', message.linear.x, message.angular.z))


class Logger:
    def __init__(self):
        self.entries = []

    def info(self, message, **kwargs):
        self.entries.append(('info', message))

    def warn(self, message, **kwargs):
        self.entries.append(('warn', message))

    def error(self, message, **kwargs):
        self.entries.append(('error', message))


class Node:
    overrides = {}
    instances = []

    def __init__(self, name):
        self.name = name
        self.clock_ns = 10_000_000_000
        self.logger = Logger()
        self.subscriptions = []
        self.timers = []
        self.destroy_count = 0
        self.instances.append(self)

    def declare_parameter(self, name, default):
        return NS(value=self.overrides.get(name, default))

    def create_subscription(self, kind, topic, callback, qos, **kwargs):
        self.subscriptions.append((topic, callback, qos))

    def create_publisher(self, kind, topic, depth):
        return Publisher(topic)

    def create_timer(self, period, callback, **kwargs):
        timer = NS(period=period, callback=callback, **kwargs)
        self.timers.append(timer)
        return timer

    def get_logger(self):
        return self.logger

    def get_clock(self):
        return NS(now=lambda: NS(nanoseconds=self.clock_ns))

    def destroy_node(self):
        self.destroy_count += 1
        EVENTS.append(('destroy',))


class QoSProfile:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TransformException(Exception):
    pass


class Buffer:
    def __init__(self):
        self.transform = transform()
        self.error = None

    def lookup_transform(self, target, source, when):
        self.last_query = (target, source)
        if self.error is not None:
            raise self.error
        return self.transform


class TransformListener:
    def __init__(self, buffer, node):
        pass

    def unregister(self):
        EVENTS.append(('listener_stop',))


class Executor:
    spin_error = KeyboardInterrupt()
    instances = []

    def __init__(self, num_threads):
        self.num_threads = num_threads
        self.shutdown_count = 0
        self.instances.append(self)

    def add_node(self, node):
        self.node = node

    def spin(self):
        if self.spin_error is not None:
            raise self.spin_error

    def shutdown(self):
        self.shutdown_count += 1
        EVENTS.append(('executor_stop',))


class ExternalShutdownException(Exception):
    pass


class CameraThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def join(self, timeout):
        EVENTS.append(('camera_join',))


def module(name, **members):
    result = types.ModuleType(name)
    result.__dict__.update(members)
    return result


def ros_init(**kwargs):
    ROS.active = True
    EVENTS.append(('ros_init', kwargs))


def ros_shutdown():
    ROS.active = False
    EVENTS.append(('ros_shutdown',))


FAKES = {
    'numpy': module('numpy'),
    'rclpy': module('rclpy', init=ros_init, try_shutdown=ros_shutdown,
                    ok=lambda: ROS.active),
    'rclpy.node': module('rclpy.node', Node=Node),
    'rclpy.callback_groups': module('rclpy.callback_groups',
                                    MutuallyExclusiveCallbackGroup=type('Group', (), {})),
    'rclpy.executors': module('rclpy.executors', MultiThreadedExecutor=Executor,
                              ExternalShutdownException=ExternalShutdownException),
    'rclpy.qos': module('rclpy.qos', QoSProfile=QoSProfile,
                        DurabilityPolicy=NS(TRANSIENT_LOCAL='transient_local'),
                        ReliabilityPolicy=NS(RELIABLE='reliable', BEST_EFFORT='best_effort')),
    'rclpy.signals': module('rclpy.signals', SignalHandlerOptions=NS(NO='no_handlers')),
    'rclpy.time': module('rclpy.time', Time=type('Time', (), {})),
    'tf2_ros': module('tf2_ros', Buffer=Buffer, TransformListener=TransformListener,
                      TransformException=TransformException),
    'geometry_msgs.msg': module('geometry_msgs.msg', Twist=Twist,
                                PoseStamped=type('PoseStamped', (), {}),
                                PoseWithCovarianceStamped=type('PoseWithCovarianceStamped', (), {})),
    'nav_msgs.msg': module('nav_msgs.msg', OccupancyGrid=type('OccupancyGrid', (), {}),
                          Path=type('Path', (), {})),
    'sensor_msgs.msg': module('sensor_msgs.msg', LaserScan=type('LaserScan', (), {}),
                             Image=type('Image', (), {}),
                             CompressedImage=type('CompressedImage', (), {})),
    'cv2': module('cv2', setNumThreads=lambda count: EVENTS.append(('opencv_threads', count)),
                  destroyAllWindows=lambda: None),
    'cv_bridge': module('cv_bridge', CvBridge=type('CvBridge', (), {})),
    'ultralytics': module('ultralytics', YOLO=lambda path: NS(names={0: 'person'})),
    'torch': module('torch', set_num_threads=lambda count: EVENTS.append(('torch_threads', count))),
}
for parent in ('geometry_msgs', 'nav_msgs', 'sensor_msgs'):
    FAKES[parent] = module(parent, __path__=[])

# Load private module names so these mocks do not replace the production package
# or leak fake ROS/NumPy modules into unrelated tests in the same process.
PACKAGE_DIR = Path(__file__).resolve().parents[1] / 'fall_patrol_navigation'
PRIVATE_PACKAGE = '_fall_patrol_navigation_test'
FAKES[PRIVATE_PACKAGE] = module(PRIVATE_PACKAGE, __path__=[str(PACKAGE_DIR)])
with patch.dict(sys.modules, FAKES):
    core = importlib.import_module(PRIVATE_PACKAGE + '.smooth_base')
    navigation = importlib.import_module(PRIVATE_PACKAGE + '.navigation_node')


class NavigationTests(unittest.TestCase):
    def setUp(self):
        EVENTS.clear()
        ROS.active = True
        Node.overrides = {}
        Node.instances.clear()
        Executor.instances.clear()
        Executor.spin_error = KeyboardInterrupt()
        self.modules = patch.dict(sys.modules, FAKES)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.monotonic = patch.object(navigation.time, 'monotonic', return_value=100.0).start()
        self.addCleanup(patch.stopall)

    def make_node(self):
        with patch.object(core.AStarDriver, 'setup_estop'):
            node = navigation.NavigationNode()
        self.addCleanup(node.shutdown_navigation)
        node.pose_callback(pose_message())
        node.scan_time = node.last_frame_time = node.det_frame_time = 100.0
        node.all_dist = node.front_dist = 10.0
        node.camera_stale = node.inference_stale = False
        return node

    def assert_stopped(self, node):
        message = node.cmd_pub.messages[-1]
        self.assertEqual((message.linear.x, message.angular.z), (0.0, 0.0))

    def test_waits_for_first_tf_before_planning(self):
        node = self.make_node()
        node.goal_pose = [1.0, 0.0]
        node.need_plan = True
        with patch.object(node, 'plan') as plan:
            node.control_loop()
        plan.assert_not_called()
        self.assert_stopped(node)

    def test_fresh_tf_and_pose_allow_original_motion_checks(self):
        node = self.make_node()
        node.check_localization()
        self.assertFalse(node.motion_blocked())
        self.assertEqual(node.tf_buffer.last_query, ('map', 'base_footprint'))

    def test_fresh_tf_without_amcl_pose_still_blocks(self):
        node = self.make_node()
        node.current_pose = None
        node.check_localization()
        self.assertTrue(node.motion_blocked())

    def test_sparse_amcl_messages_do_not_expire_with_fresh_tf(self):
        node = self.make_node()
        node.clock_ns = 100_000_000_000
        node.tf_buffer.transform = transform(node.clock_ns)
        node.check_localization()
        self.assertFalse(node.motion_blocked())

    def test_lookup_failure_stops_immediately(self):
        node = self.make_node()
        node.check_localization()
        node.last_cmd_v = 0.15
        node.tf_buffer.error = TransformException('no transform')
        node.check_localization()
        self.assertTrue(node.motion_blocked())
        self.assert_stopped(node)
        self.assertEqual(node.last_cmd_v, 0.0)

    def test_stale_or_future_tf_is_rejected(self):
        for ns in (8_000_000_000, 11_000_000_000, 0):
            with self.subTest(stamp=ns):
                node = self.make_node()
                node.tf_buffer.transform = transform(ns)
                node.check_localization()
                self.assertTrue(node.motion_blocked())
                self.assert_stopped(node)

    def test_small_future_tolerance_is_supported(self):
        node = self.make_node()
        node.tf_buffer.transform = transform(10_050_000_000)
        node.check_localization()
        self.assertFalse(node.motion_blocked())

    def test_cached_tf_expires_without_another_guard_callback(self):
        node = self.make_node()
        node.check_localization()
        node.clock_ns += 1_100_000_000
        node.publish_smooth(0.1, 0.0)
        self.assert_stopped(node)

    def test_stalled_guard_is_rejected_even_if_ros_clock_pauses(self):
        node = self.make_node()
        node.check_localization()
        self.monotonic.return_value = 102.0
        self.assertTrue(node.motion_blocked())

    def test_invalid_transform_values_are_rejected(self):
        for invalid in ('nan', 'zero_quaternion'):
            with self.subTest(value=invalid):
                node = self.make_node()
                if invalid == 'nan':
                    node.tf_buffer.transform.transform.translation.x = float('nan')
                else:
                    node.tf_buffer.transform.transform.rotation.w = 0.0
                node.check_localization()
                self.assertTrue(node.motion_blocked())

    def test_invalid_pose_is_rejected_and_can_recover(self):
        node = self.make_node()
        node.check_localization()
        for invalid in ('frame', 'nan', 'zero_quaternion'):
            with self.subTest(value=invalid):
                message = pose_message()
                if invalid == 'frame':
                    message.header.frame_id = 'odom'
                elif invalid == 'nan':
                    message.pose.pose.position.x = float('nan')
                else:
                    message.pose.pose.orientation.w = 0.0
                node.pose_callback(message)
                self.assertTrue(node.motion_blocked())
                self.assert_stopped(node)
                node.pose_callback(pose_message())
                self.assertFalse(node.motion_blocked())

    def test_existing_safety_conditions_keep_priority(self):
        for condition in ('estop_active', 'vision_fault', 'camera_stale', 'inference_stale'):
            with self.subTest(condition=condition):
                node = self.make_node()
                node.check_localization()
                setattr(node, condition, True)
                node.publish_smooth(0.1, 0.1)
                self.assert_stopped(node)
        node = self.make_node()
        node.check_localization()
        node.scan_time = 99.0
        node.publish_smooth(0.1, 0.1)
        self.assert_stopped(node)

    def test_recovery_preserves_goal_and_does_not_create_an_event(self):
        node = self.make_node()
        node.goal_pose = [1.0, 2.0]
        node.tf_buffer.error = TransformException('missing')
        node.check_localization()
        node.tf_buffer.error = None
        node.check_localization()
        self.assertEqual(node.goal_pose, [1.0, 2.0])
        self.assertFalse(node.motion_blocked())
        self.assertFalse(hasattr(node, 'web_thread'))

    def test_repeated_tf_error_does_not_repeat_warning(self):
        node = self.make_node()
        node.tf_buffer.error = TransformException('missing')
        node.check_localization()
        first = list(node.logger.entries)
        node.check_localization()
        self.assertEqual(node.logger.entries, first)

    def test_model_parameter_expands_home_directory(self):
        Node.overrides = {'model_path': '~/navigation-model.pt'}
        node = self.make_node()
        self.assertEqual(node.model_path, os.path.expanduser('~/navigation-model.pt'))

    def test_missing_model_blocks_without_calling_model_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            Node.overrides = {'model_path': str(Path(directory) / 'missing.pt')}
            with patch.object(core, 'YOLO') as loader:
                node = navigation.NavigationNode()
        self.addCleanup(node.shutdown_navigation)
        loader.assert_not_called()
        self.assertTrue(node.vision_fault)
        self.assert_stopped(node)

    def test_empty_model_path_blocks_without_downloading(self):
        with patch.object(core, 'YOLO') as loader:
            node = navigation.NavigationNode()
        self.addCleanup(node.shutdown_navigation)
        loader.assert_not_called()
        self.assertEqual(node.model_path, '')
        self.assertTrue(node.vision_fault)

    def test_existing_model_path_keeps_threaded_vision_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.pt'
            path.write_bytes(b'not a real model; loader is mocked')
            Node.overrides = {'model_path': str(path)}
            with patch.object(core, 'YOLO', return_value=NS(names={0: 'person'})) as loader:
                with patch.object(core.threading, 'Thread', CameraThread):
                    node = navigation.NavigationNode()
            loader.assert_called_once_with(str(path))
        self.addCleanup(node.shutdown_navigation)
        self.assertTrue(node.yolo_thread.started)
        self.assertIn(('torch_threads', 2), EVENTS)
        self.assertIn(('opencv_threads', 1), EVENTS)

    def test_invalid_guard_parameters_clean_up_constructed_node(self):
        for name, value in (('localization_max_age_sec', 0),
                            ('localization_max_age_sec', float('nan')),
                            ('localization_future_tolerance_sec', -1),
                            ('base_frame', '')):
            with self.subTest(parameter=name, value=value):
                Node.overrides = {name: value}
                with patch.object(core.AStarDriver, 'setup_estop'):
                    with self.assertRaises(ValueError):
                        navigation.NavigationNode()
                node = Node.instances[-1]
                self.assertEqual(node.destroy_count, 1)
                self.assert_stopped(node)

    def test_existing_topics_qos_and_control_period_are_preserved(self):
        node = self.make_node()
        subscriptions = {topic: qos for topic, callback, qos in node.subscriptions}
        self.assertEqual(subscriptions['/map'].durability, 'transient_local')
        self.assertEqual(subscriptions['/amcl_pose'].durability, 'transient_local')
        self.assertEqual(subscriptions['/scan'].reliability, 'best_effort')
        control = next(timer for timer in node.timers if timer.callback == node.control_loop)
        guard = next(timer for timer in node.timers if timer.callback == node.check_localization)
        self.assertEqual(control.period, 0.05)
        self.assertEqual(guard.period, 0.1)
        self.assertIsNotNone(guard.callback_group)

    def test_shutdown_stops_before_executor_and_releases_resources_once(self):
        node = self.make_node()
        node.yolo_thread = CameraThread()
        executor = Executor(3)
        EVENTS.clear()
        node.shutdown_navigation(executor)
        self.assertEqual(EVENTS[0], ('velocity', 0.0, 0.0))
        self.assertLess(EVENTS.index(('executor_stop',)), EVENTS.index(('camera_join',)))
        self.assertLess(EVENTS.index(('camera_join',)), EVENTS.index(('destroy',)))
        self.assertTrue(node.frame_event.is_set())
        node.shutdown_navigation(executor)
        self.assertEqual(node.destroy_count, 1)
        self.assertEqual(executor.shutdown_count, 1)

    def test_main_ctrl_c_keeps_ros_alive_until_stop(self):
        node = self.make_node()
        EVENTS.clear()
        with patch.object(navigation, 'NavigationNode', return_value=node):
            self.assertEqual(navigation.main(), 0)
        self.assertEqual(Executor.instances[-1].num_threads, 3)
        self.assertEqual(EVENTS[0][1]['signal_handler_options'], 'no_handlers')
        self.assertLess(EVENTS.index(('velocity', 0.0, 0.0)), EVENTS.index(('ros_shutdown',)))

    def test_main_runtime_error_still_stops_and_cleans_up(self):
        node = self.make_node()
        Executor.spin_error = RuntimeError('executor failure')
        with patch.object(navigation, 'NavigationNode', return_value=node):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(navigation.main(), 1)
        self.assertTrue(node._shutdown_complete)
        self.assertFalse(ROS.active)

    def test_main_constructor_error_shuts_down_ros(self):
        with patch.object(navigation, 'NavigationNode', side_effect=ValueError('bad parameter')):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(navigation.main(), 1)
        self.assertFalse(ROS.active)
        self.assertEqual(Executor.instances, [])


class Grid:
    def __init__(self, rows):
        self.rows = rows
        self.shape = (len(rows), len(rows[0]))

    def __getitem__(self, key):
        if isinstance(key, tuple):
            return self.rows[key[0]][key[1]]
        return self.rows[key]


class CoreMathTests(unittest.TestCase):
    def test_negative_world_coordinate_uses_floor(self):
        self.assertEqual(core.world_to_grid(-0.01, 0.0, [0.0, 0.0], 0.05), (0, -1))

    def test_astar_does_not_cut_a_blocked_diagonal_corner(self):
        grid = Grid([[False, True], [True, False]])
        self.assertIsNone(core.run_astar(grid, (0, 0), (1, 1)))

    def test_astar_routes_around_a_wall(self):
        grid = Grid([[False, True, False], [False, True, False], [False, False, False]])
        route = core.run_astar(grid, (0, 0), (0, 2))
        self.assertEqual(route[0], (0, 0))
        self.assertEqual(route[-1], (0, 2))
        self.assertIn((2, 1), route)

    def test_speed_change_and_direction_reversal_are_limited(self):
        self.assertAlmostEqual(core.slew(0.0, 0.15, 0.12, 0.2, 0.05), 0.006)
        self.assertAlmostEqual(core.slew(0.1, -0.1, 0.12, 0.2, 0.05), 0.09)

    def test_tracking_segment_cannot_cross_a_wall(self):
        grid = Grid([[False, True, False]])
        self.assertFalse(core.segment_clear(grid, (0.5, 0.5), (2.5, 0.5), [0, 0], 1.0))


if __name__ == '__main__':
    unittest.main()
