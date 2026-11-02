"""Exercise event-photo rendering and matching without a camera or ROS2."""

import importlib.util
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace as O
import unittest
from unittest.mock import Mock, patch

from test_patrol import fake_node, ns, person, pose


PACKAGE = Path(__file__).resolve().parents[1] / 'my_bot_fall'


def overlay_module():
    cv = O(error=ValueError, IMREAD_COLOR=1, IMWRITE_JPEG_QUALITY=1,
           FONT_HERSHEY_SIMPLEX=0, LINE_AA=16,
           imdecode=Mock(return_value=O(shape=(100, 200, 3))),
           imencode=Mock(return_value=(True, O(tobytes=lambda: b'annotated JPEG'))),
           rectangle=Mock(), line=Mock(), circle=Mock(), putText=Mock(),
           getTextSize=Mock(return_value=((20, 8), 0)))
    numpy = O(uint8=object(), frombuffer=lambda data, dtype: data)
    spec = importlib.util.spec_from_file_location('photo_overlay_test', PACKAGE / 'event_photo_overlay.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'cv2': cv, 'numpy': numpy}):
        spec.loader.exec_module(module)
    return module, cv


def annotation():
    points = [None] * 17
    points[5], points[6] = [20, 15], [30, 15]
    return dict(image_size=[100, 50], box=[10, 5, 90, 45], keypoints=points)


class OverlayTests(unittest.TestCase):
    def test_labels_scale_coordinates_and_draw_only_supplied_joints(self):
        for result, color in [('confirmed', (48, 48, 255)), ('uncertain', (0, 149, 255))]:
            with self.subTest(result=result):
                module, cv = overlay_module()
                self.assertEqual(module.annotate_event_photo(b'JPEG', annotation(), result),
                                 b'annotated JPEG')
                self.assertEqual(cv.rectangle.call_args_list[0].args[1:4],
                                 ((20, 10), (180, 90), color))
                self.assertEqual(cv.line.call_count, 1)
                self.assertEqual(cv.circle.call_count, 2)
                self.assertEqual(cv.circle.call_args_list[0].args[1], (40, 30))

    def test_missing_geometry_keeps_original_and_invalid_joints_are_skipped(self):
        module, cv = overlay_module()
        self.assertEqual(module.annotate_event_photo(b'JPEG', {}, 'confirmed'), b'JPEG')
        cv.rectangle.assert_not_called()
        data = annotation()
        data['keypoints'] = [[float('nan'), 0], [500, 500]] + [None] * 15
        module.annotate_event_photo(b'JPEG', data, 'uncertain')
        cv.circle.assert_not_called()
        cv.line.assert_not_called()

    def test_invalid_jpeg_or_encoding_failure_reports_error(self):
        module, cv = overlay_module()
        cv.imdecode.return_value = None
        with self.assertRaises(ValueError):
            module.annotate_event_photo(b'bad JPEG', annotation(), 'confirmed')
        cv.imdecode.return_value = O(shape=(100, 200, 3))
        cv.imencode.return_value = (False, None)
        with self.assertRaises(ValueError):
            module.annotate_event_photo(b'JPEG', annotation(), 'confirmed')

    def test_non_alert_result_does_not_decode_or_change_photo(self):
        module, cv = overlay_module()
        self.assertEqual(module.annotate_event_photo(b'JPEG', annotation(), 'false_alarm'), b'JPEG')
        cv.imdecode.assert_not_called()


class PatrolPhotoTests(unittest.TestCase):
    def node(self):
        n = fake_node()
        self.addCleanup(shutil.rmtree, n.journal.parent)
        n.clear_observation_image()
        n.logic.phase = 'observe'
        n.logic.started = time.monotonic() - .1
        n.observation_started = n.logic.started
        n.logic.target = person(pose_frame='map')
        watched = person(box=[10, 5, 90, 45], keypoints=annotation()['keypoints'])
        other = person(target_id=2, map_x=20., box=[0, 0, 5, 5])
        n.pose_data = pose()
        n.pose_data.update(people=[other, watched], image_size=[100, 50], image_stamp=time.time())
        n.image_jpeg = b'latest live JPEG'
        n.image_seen = n.pose_seen = time.monotonic()
        n.recent_images.append((n.pose_data['image_stamp'], b'matching target JPEG', n.pose_seen - .01))
        return n

    def make_incident(self, n, renderer):
        n.logic.phase, n.logic.result = 'alert', 'confirmed'
        with patch.dict(ns, {'annotate_event_photo': renderer, 'cv2': O(error=ValueError)}):
            n.make_incident()

    def test_matched_target_overlay_does_not_change_live_jpeg(self):
        n = self.node()
        n.capture_observation_image()
        renderer = Mock(return_value=b'annotated JPEG')
        self.make_incident(n, renderer)
        self.assertEqual(renderer.call_args.args[0], b'matching target JPEG')
        self.assertEqual(renderer.call_args.args[1]['box'], [10, 5, 90, 45])
        self.assertEqual(n.incident['image'], b'annotated JPEG')
        self.assertEqual(n.image_jpeg, b'latest live JPEG')
        self.assertEqual(n.incident['result'], 'confirmed')

    def test_frame_mismatch_keeps_original_photo_without_overlay(self):
        n = self.node()
        n.pose_data['image_stamp'] -= 2
        n.capture_observation_image()
        renderer = Mock()
        self.make_incident(n, renderer)
        renderer.assert_not_called()
        self.assertEqual(n.incident['image'], b'latest live JPEG')

    def test_rendering_failure_keeps_event_and_original_photo(self):
        n = self.node()
        n.capture_observation_image()
        self.make_incident(n, Mock(side_effect=ValueError('drawing failed')))
        self.assertEqual(n.incident['image'], b'matching target JPEG')
        self.assertEqual(n.incident['result'], 'confirmed')

    def test_old_observation_cannot_reuse_annotation_or_photo(self):
        n = self.node()
        n.capture_observation_image()
        self.assertIsNotNone(n.observation_annotation)
        n.clear_observation_image()
        self.assertIsNone(n.observation_annotation)
        self.assertIsNone(n.observation_image)

    def test_camera_callback_caches_source_stamp_and_jpeg(self):
        n = self.node()
        stamp = time.time()
        sec = int(stamp)
        nanos = int((stamp - sec) * 1e9)
        n.get_clock = lambda: O(now=lambda: O(nanoseconds=int(stamp * 1e9)))
        n.image_callback(O(header=O(stamp=O(sec=sec, nanosec=nanos)),
                           format='jpeg', data=b'new camera JPEG'))
        self.assertEqual(n.recent_images[-1][1], b'new camera JPEG')
        self.assertAlmostEqual(n.recent_images[-1][0], stamp, places=5)


if __name__ == '__main__':
    unittest.main()
