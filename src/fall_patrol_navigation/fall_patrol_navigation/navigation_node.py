"""Run the navigation core with a fresh map-to-base TF requirement."""

import math
import sys
import time

import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.signals import SignalHandlerOptions
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener

from .smooth_base import AStarDriver


class NavigationNode(AStarDriver):
    """Keep the AMCL input and use TF health to gate all velocity commands."""

    def __init__(self):
        self._shutdown_complete = False
        self._tf_valid = False
        self._tf_stamp_ns = None
        self._tf_checked_at = None
        self._last_tf_error = None
        self._pose_invalid = False
        try:
            super().__init__()
            self.base_frame = self.declare_parameter('base_frame', 'base_footprint').value
            self.localization_max_age_sec = float(
                self.declare_parameter('localization_max_age_sec', 1.0).value)
            self.localization_future_tolerance_sec = float(
                self.declare_parameter('localization_future_tolerance_sec', 0.1).value)
            if not isinstance(self.base_frame, str) or not self.base_frame.strip():
                raise ValueError('base_frame must be a nonempty frame name')
            if (not math.isfinite(self.localization_max_age_sec)
                    or self.localization_max_age_sec <= 0):
                raise ValueError('localization_max_age_sec must be finite and positive')
            if (not math.isfinite(self.localization_future_tolerance_sec)
                    or self.localization_future_tolerance_sec < 0):
                raise ValueError('localization_future_tolerance_sec must be finite and nonnegative')
            self.tf_buffer = Buffer()
            self.tf_listener = TransformListener(self.tf_buffer, self)
            self.localization_group = MutuallyExclusiveCallbackGroup()
            self.create_timer(0.1, self.check_localization,
                              callback_group=self.localization_group)
            self.stop_robot()
        except BaseException:
            self.shutdown_navigation()
            raise

    def pose_callback(self, msg):
        try:
            pose = msg.pose.pose
            q = pose.orientation
            values = (pose.position.x, pose.position.y, q.x, q.y, q.z, q.w)
            valid = (msg.header.frame_id == 'map'
                     and all(math.isfinite(value) for value in values)
                     and sum(value * value for value in values[2:]) > 1e-12)
        except (AttributeError, TypeError, ValueError):
            valid = False
        with self.command_lock:
            if not valid:
                self.current_pose = None
                self.stop_robot()
                if not self._pose_invalid:
                    self.get_logger().warn('Invalid AMCL pose: waiting for a finite map-frame pose')
                self._pose_invalid = True
                return
            self._pose_invalid = False
            super().pose_callback(msg)

    def _tf_is_current(self):
        if not self._tf_valid or self._tf_stamp_ns is None or self._tf_checked_at is None:
            return False
        if time.monotonic() - self._tf_checked_at > self.localization_max_age_sec:
            return False
        age = (self.get_clock().now().nanoseconds - self._tf_stamp_ns) * 1e-9
        return (-self.localization_future_tolerance_sec <= age
                <= self.localization_max_age_sec)

    def check_localization(self):
        if self.closing:
            return
        try:
            # A zero-timeout lookup uses the latest cached TF without blocking control.
            transform = self.tf_buffer.lookup_transform('map', self.base_frame, Time())
            stamp = transform.header.stamp
            stamp_ns = stamp.sec * 1_000_000_000 + stamp.nanosec
            t, q = transform.transform.translation, transform.transform.rotation
            values = (t.x, t.y, t.z, q.x, q.y, q.z, q.w)
            if (stamp_ns <= 0 or not all(math.isfinite(value) for value in values)
                    or sum(value * value for value in values[3:]) <= 1e-12):
                raise ValueError('Invalid transform data')
            with self.command_lock:
                self._tf_valid = True
                self._tf_stamp_ns = stamp_ns
                self._tf_checked_at = time.monotonic()
                if not self._tf_is_current():
                    raise ValueError('Transform timestamp is stale or too far in the future')
                if self._last_tf_error is not None:
                    self.get_logger().info('Localization TF recovered')
                self._last_tf_error = None
        except (TransformException, AttributeError, TypeError, ValueError) as error:
            with self.command_lock:
                self._tf_valid = False
                self._tf_stamp_ns = None
                self._tf_checked_at = None
                self.stop_robot()
                reason = str(error)
                if reason != self._last_tf_error:
                    self.get_logger().warn(
                        'Localization TF unavailable: ' + reason, throttle_duration_sec=5.0)
                    self._last_tf_error = reason

    def motion_blocked(self):
        with self.command_lock:
            return (self.current_pose is None or not self._tf_is_current()
                    or super().motion_blocked())

    def shutdown_navigation(self, executor=None):
        """Stop while ROS is live, finish callbacks and release the camera worker."""
        if self._shutdown_complete:
            return
        self.closing = True
        try:
            if hasattr(self, 'cmd_pub') and rclpy.ok():
                self.stop_robot()
        finally:
            try:
                if executor is not None:
                    executor.shutdown()
            finally:
                try:
                    if hasattr(self, 'frame_event'):
                        self.close_camera()
                finally:
                    try:
                        if hasattr(self, 'tf_listener'):
                            self.tf_listener.unregister()
                    finally:
                        self.destroy_node()
                        self._shutdown_complete = True


def main(args=None):
    # Keep ROS alive on Ctrl+C so the final zero command precedes shutdown.
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = None
    executor = None
    result = 0
    try:
        node = NavigationNode()
        executor = MultiThreadedExecutor(num_threads=3)
        executor.add_node(node)
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception as error:
        print('Navigation failed: ' + str(error), file=sys.stderr)
        result = 1
    finally:
        try:
            if node is not None:
                node.shutdown_navigation(executor)
            elif executor is not None:
                executor.shutdown()
        finally:
            rclpy.try_shutdown()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
