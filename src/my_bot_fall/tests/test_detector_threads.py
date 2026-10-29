"""Exercise actual detector callback methods with fake inference/ROS messages."""
import ast
from collections import deque
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace as O
import unittest


root=Path(__file__).resolve().parents[1]
tree=ast.parse((root/'my_bot_fall/lying_detector_node.py').read_text())
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LyingDetectorNode')
names={'on_image','on_scan','on_info','watchdog','publish_unavailable','image_problem','log_still_progress','tick'}
methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
cv=O(error=ValueError,IMWRITE_JPEG_QUALITY=1,imencode=lambda *a:(False,None))
ns=dict(time=time,json=json,cv2=cv,String=lambda **kw:O(**kw))
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='Harness',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])),'detector-methods','exec'),ns)
Harness=ns['Harness']


def node():
    n=Harness();n.sensor_lock=threading.Lock();n.reset_requested=threading.Event()
    n.camera_timeout=.2;n.last_raw_mono=0.;n.last_received_mono=time.monotonic()
    n.last_result_mono=n.inference_started=time.monotonic();n.last_image_stamp=None
    n.latest_img=None;n.last_img_time=0;n.last_health_reason=None;n.rejected_image_reason='waiting'
    n.pending_calibration=None;n.pending_map=None;n.scans=deque();n.fy_fixed=False
    n.stamp_sec=lambda m:m.stamp
    n.get_clock=lambda:O(now=lambda:O(nanoseconds=int(time.time()*1e9)))
    n.get_logger=lambda:O(warn=lambda *a:None,info=lambda *a:None,error=lambda *a:None)
    n.pose_frame='map';n.tf_frames=('map','odom','base_footprint');n.last_scan_time=n.start_time=time.time();n.last_scan_warn=0
    n.closest_scan=lambda t:None;n.lookup_pose=lambda:(0,0,0);n.to_cv=lambda msg:O(size=12)
    n.core=O(fall_after_sec=10,reset_observation=lambda:None,process=lambda *a:({'state':'none'},O(size=12)))
    n._progress_log_at={};n._progress_log_state={};n.progress_log_interval=1.0
    n.cam=O(set_calibration=lambda *a:None);n.show=False;n.frames=0;n.fps_t0=time.time()
    n.pub_debug=O(get_subscription_count=lambda:0)
    n.messages=[];n.pub_state=O(publish=lambda m:n.messages.append(json.loads(m.data)))
    return n


class DetectorTests(unittest.TestCase):
    def test_receive_does_not_wait_for_inference_and_recovers(self):
        n=node();entered=threading.Event();release=threading.Event()
        def slow(*args):
            entered.set();release.wait(2);return {'state':'none'},O(size=12)
        n.core.process=slow
        n.on_image(O(stamp=time.time()));worker=threading.Thread(target=n.tick);worker.start()
        self.assertTrue(entered.wait(1))
        started=time.monotonic();n.on_image(O(stamp=time.time()+.001))
        self.assertLess(time.monotonic()-started,.1)
        self.assertIsNotNone(n.latest_img)
        # Force an independent health timeout without touching the core in the callback.
        n.last_result_mono-=1;n.inference_started-=1;n.watchdog()
        self.assertEqual(n.messages[-1]['camera_status'],'inference_stale')
        release.set();worker.join(2);self.assertFalse(worker.is_alive())
        self.assertFalse(n.messages[-1]['observation_valid'])
        n.core.process=lambda *a:({'state':'none','camera_status':'ok','observation_valid':True},O(size=12))
        n.on_image(O(stamp=time.time()+.002));n.tick()
        self.assertTrue(n.messages[-1]['observation_valid'])

    def test_frame_age_not_hidden_by_new_frame(self):
        n=node();n.on_image(O(stamp=time.time()))
        self.assertEqual(n.image_problem(O(stamp=time.time()),time.monotonic()-1),'frame_processing_delay')

    def test_clock_mismatch_distinguished(self):
        n=node();self.assertEqual(n.image_problem(O(stamp=time.time()-30),time.monotonic()),'timestamp_mismatch')

    def test_duplicates_do_not_refresh_accepted_frame(self):
        n=node();m=O(stamp=time.time());n.on_image(m);old=n.last_received_mono;n.on_image(m)
        self.assertEqual(n.last_received_mono,old);self.assertEqual(n.rejected_image_reason,'duplicate_or_old_stamp')

    def test_no_frames_diagnostic(self):
        n=node();n.watchdog();self.assertEqual(n.messages[-1]['camera_status'],'no_frames')


if __name__=='__main__':unittest.main(verbosity=2)
