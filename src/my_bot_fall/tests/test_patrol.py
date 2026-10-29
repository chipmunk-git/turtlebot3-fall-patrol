"""Offline tests: real FastAPI + production state machine; ROS movement uses a test double."""
import ast
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT/'my_bot_fall'
spec = importlib.util.spec_from_file_location('patrol_logic', PKG/'patrol_logic.py')
logic_module = importlib.util.module_from_spec(spec);spec.loader.exec_module(logic_module)
PatrolLogic = logic_module.PatrolLogic
spec = importlib.util.spec_from_file_location('voice_link', PKG/'voice_link.py')
voice_module = importlib.util.module_from_spec(spec);spec.loader.exec_module(voice_module)


def person(**kw):
    return dict(dict(target_id=1,raw='lying',state='lying',fallen=False,
                     observation_valid=True,still_sec=0,map_x=2.,map_y=0.),**kw)


def pose(p=None):
    return dict(people=[] if p is None else [p],pose_frame='map',camera_status='ok',
                observation_valid=True,stamp=time.time(),fall_after_sec=10.)


class LogicTests(unittest.TestCase):
    def new(self):
        x=PatrolLogic();x.phase='patrol';return x

    def test_lie_observe_confirm_ack_resume(self):
        x=self.new();x.update(pose(person()),0);self.assertEqual(x.phase,'observe')
        x.update(pose(person(state='fallen',fallen=True,still_sec=10)),11)
        self.assertEqual((x.phase,x.result),('alert','confirmed'))
        x.acknowledge();self.assertEqual(x.phase,'ack_wait')
        x.resume(12);self.assertEqual(x.phase,'patrol')
        x.update(pose(person(state='fallen',fallen=True)),13);self.assertEqual(x.phase,'patrol')
        x.update(pose(person(target_id=2,map_x=4)),14);self.assertEqual(x.phase,'observe')

    def test_disappear_uncertain_not_confirmed(self):
        x=self.new();x.update(pose(person()),0);x.update(pose(),8);x.tick(26,True)
        self.assertEqual((x.phase,x.result),('alert','uncertain'))

    def test_normal_requires_continuous_observation(self):
        x=self.new();x.update(pose(person()),0)
        x.update(pose(person(raw='not_lying',state='normal')),1)
        x.tick(2,False)
        x.update(pose(person(raw='not_lying',state='normal')),3)
        self.assertEqual(x.phase,'observe')
        x.update(pose(person(raw='not_lying',state='normal')),5.1)
        self.assertEqual(x.phase,'patrol')

    def test_cooldown_expires(self):
        x=self.new();x.target=person(pose_frame='map');x.resume(0)
        x.update(pose(person()),29);self.assertEqual(x.phase,'patrol')
        x.update(pose(person()),31);self.assertEqual(x.phase,'observe')

    def test_hidden_never_confirms(self):
        x=self.new();d=pose();d['hidden']=[person(state='fallen',fallen=True)]
        x.update(d,0);self.assertEqual(x.phase,'patrol')


class FakeBase:
    def motion_blocked(self):return (self.base_blocked_test or self.estop_active or
                                     time.monotonic()-self.scan_time > self.scan_timeout)
    def control_loop(self):pass
    def stop_robot(self):self.stops+=1


src=ast.parse((PKG/'fall_patrol.py').read_text())
cls=next(n for n in src.body if isinstance(n,ast.ClassDef))
ns=dict(AStarDriver=FakeBase,PatrolLogic=PatrolLogic,math=math,time=time,json=json,uuid=uuid,
        Path=Path,os=os,Time=lambda:None,append_note=voice_module.append_note,voice_note=voice_module.voice_note)
exec(compile(ast.Module(body=[cls],type_ignores=[]),'production-FallPatrol','exec'),ns)
FallPatrol=ns['FallPatrol']


def fake_node():
    n=FallPatrol.__new__(FallPatrol)
    n.logic=PatrolLogic();n.logic.phase='patrol';n.command_lock=threading.RLock()
    n.voice_enabled=False
    n.get_logger=lambda: type('Logger', (), {'warn': lambda *a: None, 'info': lambda *a: None})()
    n.pose_seen=n.server_seen=n.last_frame_time=n.image_seen=time.monotonic()
    n.localization_seen=n.raw_rx_seen=n.scan_time=time.monotonic()
    n.localization_timeout=2.;n.location_source='TF';n.image_reject='ok'
    n.scan_timeout=.6;n.all_dist=2.;n.stop_dist=.2;n.vision_fault=False
    n.pose_stamp=time.time();n.pose_timeout=n.frame_timeout=1.5;n.pose_data=pose()
    n.base_blocked_test=False;n.stops=0;n.estop_active=False;n.estop_distance_threshold=.5
    n.estop_release_delay=.8;n.last_detect_time=0
    n.waypoints=[(0.,0.),(1.,0.),(1.,1.)];n.waypoint_index=0
    n.wait_until=None;n.wait_seconds=2.;n.goal_pose=(1.,0.);n.current_pose=(0.,0.);n.current_yaw=0.
    n.goal_tolerance=.12;n.incident=None;n.image_jpeg=None
    n.journal=Path(tempfile.mkdtemp())/'incident.json'
    return n


class DriverTests(unittest.TestCase):
    def test_camera_loss_stops(self):
        n=fake_node();n.pose_seen-=10;n.detect_callback();self.assertGreater(n.stops,0)
        self.assertTrue(n.motion_blocked())

    def test_visibility_flags_do_not_stop(self):
        n=fake_node();n.pose_data['back_up']=True;n.pose_data['people']=[person(too_close=True,dist=1.2,dist_src='lidar')];n.detect_callback()
        self.assertFalse(n.estop_active);self.assertEqual(n.stops,0)

    def test_measured_close_person_stops(self):
        n=fake_node();n.pose_data['people']=[person(dist=.4,dist_src='lidar')];n.detect_callback()
        self.assertTrue(n.estop_active);self.assertGreater(n.stops,0)

    def test_lost_localization_blocks_ack(self):
        n=fake_node();n.current_pose=None;n.logic.phase='ack_wait';n.control_loop()
        self.assertEqual(n.logic.phase,'ack_wait');self.assertTrue(n.motion_blocked())

    def test_ack_does_not_override_safety(self):
        n=fake_node();n.logic.phase='ack_wait';n.base_blocked_test=True;n.control_loop()
        self.assertEqual(n.logic.phase,'ack_wait')
        n.base_blocked_test=False;n.control_loop();self.assertEqual(n.logic.phase,'patrol')

    def test_ack_does_not_override_offline_web(self):
        n=fake_node();n.logic.phase='ack_wait';n.server_seen-=10;n.control_loop()
        self.assertEqual(n.logic.phase,'ack_wait')

    def test_waypoint_wrap(self):
        n=fake_node();n.waypoint_index=2;n.wait_until=time.monotonic()-1
        selected=[];n.set_patrol_goal=lambda:selected.append(n.waypoint_index)
        n.control_loop();self.assertEqual(selected,[0])

    def test_old_or_duplicate_pose_rejected(self):
        n=fake_node();old=n.pose_seen
        from types import SimpleNamespace
        d=pose(person());d['stamp']=n.pose_stamp-10
        n.pose_callback_message(SimpleNamespace(data=json.dumps(d)))
        self.assertEqual(n.pose_seen,old);self.assertEqual(n.logic.phase,'patrol')



class WebTests(unittest.TestCase):
    def test_original_gui_api_and_ack(self):
        n=fake_node();n.logic.phase='alert';n.logic.result='confirmed'
        n.incident={'token':'t','event_id':None,'result':'confirmed','x':1.,'y':2.}
        calls=[]
        def http(method,path,data=None,*a):
            calls.append((method,path,data))
            if method=='GET' and path=='/events/7':return {'status':'acknowledged','acknowledged_at':'now'}
            return {'status':'ok'}
        n.http=http;n.post_incident=lambda incident:{'id':7}
        n.web_cycle()
        self.assertEqual(n.logic.phase,'ack_wait')
        self.assertTrue(any(m=='PUT' and p=='/status' for m,p,d in calls))
        self.assertFalse(any(p=='/patrol' for m,p,d in calls))
        self.assertTrue(n.incident['note_sent'])
    def test_false_alarm_requires_sensor_clearance_before_resume(self):
        n=fake_node();n.logic.phase='alert';n.logic.result='uncertain'
        n.logic.target=person(pose_frame='map')
        n.incident={'token':'t','event_id':7,'result':'uncertain',
                    'note_sent':True,'target':n.logic.target}
        original_goal=n.goal_pose
        n.http=lambda method,path,data=None,*a: (
            {'status':'false_alarm','acknowledged_at':None} if method=='GET' and path=='/events/7'
            else {'status':'ok'})
        n.web_cycle()
        self.assertEqual(n.logic.phase,'ack_wait')
        self.assertIn('오탐',n.logic.reason)
        self.assertEqual(n.goal_pose,original_goal)
        n.base_blocked_test=True
        n.control_loop()
        self.assertEqual(n.logic.phase,'ack_wait')
        self.assertIsNotNone(n.incident)
        n.base_blocked_test=False
        n.control_loop()
        self.assertEqual(n.logic.phase,'patrol')
        self.assertEqual(n.goal_pose,original_goal)
        self.assertIsNone(n.incident)
        self.assertEqual(json.loads(n.journal.read_text()),{})
        self.assertEqual(len(n.logic.ignored),1)

    def test_pending_uncertain_or_unacknowledged_resolved_do_not_resume(self):
        for status in ('uncertain','confirmed','pending','resolved'):
            with self.subTest(status=status):
                n=fake_node();n.logic.phase='alert';n.logic.result='uncertain'
                n.incident={'token':'t','event_id':7,'result':'uncertain',
                            'note_sent':True,'target':person(pose_frame='map')}
                n.http=lambda method,path,data=None,*a: (
                    {'status':status,'acknowledged_at':None}
                    if method=='GET' and path=='/events/7' else {'status':'ok'})
                n.web_cycle()
                self.assertEqual(n.logic.phase,'alert')
                n.control_loop()
                self.assertNotEqual(n.logic.phase,'patrol')

    def test_false_alarm_does_not_bypass_no_pose_or_no_web(self):
        for broken in ('pose','web','lidar'):
            with self.subTest(broken=broken):
                n=fake_node();n.logic.phase='ack_wait'
                n.incident={'result':'uncertain','event_id':7}
                if broken=='pose':n.pose_seen-=10
                if broken=='web':n.server_seen-=10
                if broken=='lidar':n.scan_time-=10
                n.control_loop()
                self.assertEqual(n.logic.phase,'ack_wait')
                self.assertIsNotNone(n.incident)

    def test_unknown_post_not_repeated(self):
        n=fake_node();n.logic.phase='alert';n.incident={'creating':True,'event_id':None}
        n.http=lambda *a:{}
        n.post_incident=lambda *a:self.fail('must not POST twice')
        with self.assertRaises(RuntimeError):n.web_cycle()
        self.assertEqual(n.logic.phase,'error')

if __name__ == "__main__": unittest.main(verbosity=2)
