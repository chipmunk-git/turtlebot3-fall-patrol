from pathlib import Path
import queue
import sys
import threading
import tempfile
import json
from types import SimpleNamespace
from unittest.mock import patch
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from my_bot_fall.voice_node import VoiceWorker, run_process


def request(key='one', ttl=10):
    now = time.time()
    return dict(request_id=key, action='observe', issued_at=now, expires_at=now+ttl)


def alert_request(req, event_id=7):
    return dict(req, action='alert', event_id=event_id)


class WorkerTests(unittest.TestCase):
    def test_saved_request_does_not_replay_after_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'voice.json'
            req = request()
            first = VoiceWorker([], lambda *args: dict(status='ok', text='test', classification='확인 필요'), state)
            first.receive(req);first.thread.join(1)
            # Simulate a process restart with the completed result on disk.
            second = VoiceWorker([], lambda *args: self.fail('must not replay'), state)
            try:
                second.receive(req)
                self.assertEqual(second.results.get_nowait()['text'], 'test')
            finally:
                first.close();second.close()

    def test_repeated_requests_return_cached_result_without_replaying(self):
        calls = []
        def runner(command, req, cancel):
            calls.append(req['request_id'])
            return dict(status='ok', text='도와주세요', classification='도움 필요')
        worker = VoiceWorker([], runner)
        self.addCleanup(worker.close)
        req = request();worker.receive(req);worker.thread.join(1)
        worker.receive(req);worker.receive(req)
        self.assertEqual(calls, ['one'])
        self.assertEqual([worker.results.get_nowait()['request_id'] for _ in range(3)], ['one']*3)

    def test_cancellation_blocks_replay_even_if_old_request_is_delivered_later(self):
        worker = VoiceWorker([], lambda *args: self.fail('must not start'))
        self.addCleanup(worker.close)
        req = request();worker.receive(dict(req, action='cancel'));worker.receive(req)
        self.assertIsNone(worker.active)
        self.assertTrue(worker.results.empty())

    def test_cancel_stops_active_worker_and_drops_its_late_result(self):
        entered = threading.Event()
        def runner(command, req, cancel):
            entered.set();cancel.wait(1)
            return {'status': 'error', 'message': 'cancelled'}
        worker = VoiceWorker([], runner);self.addCleanup(worker.close)
        req = request();worker.receive(req);self.assertTrue(entered.wait(1))
        worker.receive(dict(req, action='cancel'));worker.thread.join(1)
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(worker.results.empty())

    def test_overlapping_requests_do_not_start_another_microphone(self):
        entered, release = threading.Event(), threading.Event()
        def runner(command, req, cancel):
            entered.set();release.wait(1);return {'status': 'error'}
        worker = VoiceWorker([], runner);self.addCleanup(worker.close)
        worker.receive(request());self.assertTrue(entered.wait(1))
        worker.receive(request('two'))
        self.assertNotIn('two', worker.jobs)
        release.set();worker.thread.join(1)

    def test_real_subprocess_json_round_trip_without_audio(self):
        result = run_process([sys.executable, '-c',
                              'import json; print(json.dumps({"status":"ok","text":"test"}))'],
                             request(), threading.Event())
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['text'], 'test')

    def test_hung_subprocess_is_terminated_at_deadline(self):
        started = time.monotonic()
        result = run_process([sys.executable, '-c', 'import time; time.sleep(30)'],
                             request(ttl=0.2), threading.Event())
        self.assertEqual(result['status'], 'error')
        self.assertLess(time.monotonic()-started, 5)

    def test_server_process_receives_original_observation_identity_and_deadline(self):
        req = request()
        command = [sys.executable, 'whisper_session.py', '--server-url', 'http://voice.test:8000']
        process = SimpleNamespace(returncode=0, poll=lambda: 0,
            communicate=lambda **kwargs: (json.dumps(dict(status='ok', text='test')), ''))
        with patch('my_bot_fall.voice_node.subprocess.Popen', return_value=process) as popen:
            self.assertEqual(run_process(command, req, threading.Event())['status'], 'ok')
        actual = popen.call_args.args[0]
        self.assertEqual(actual[-4:], ['--request-id', req['request_id'], '--expires-at', str(req['expires_at'])])
        self.assertEqual(len(command), 4)

    def test_alert_waits_for_completed_exchange_and_never_opens_another_microphone(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def runner(command, req, cancel):
            calls.append((command, req['action']))
            if req['action'] == 'observe':
                entered.set();release.wait(1)
            return dict(status='ok', text='도와주세요', classification='도움 필요')
        worker = VoiceWorker(['recognize'], runner, alert_command=['play-alert'])
        self.addCleanup(worker.close)
        req = request()
        worker.receive(alert_request(req))  # no matching observation on this Pi
        self.assertEqual(calls, [])
        worker.receive(req);self.assertTrue(entered.wait(1))
        worker.receive(alert_request(req))
        self.assertEqual(calls, [(['recognize'], 'observe')])
        release.set();worker.thread.join(1)
        original = worker.results.get_nowait()
        worker.receive(alert_request(req));worker.thread.join(1)
        ack = worker.results.get_nowait()
        self.assertEqual(calls, [(['recognize'], 'observe'), (['play-alert'], 'alert')])
        self.assertEqual((ack['action'], ack['event_id']), ('alert', 7))
        self.assertEqual(worker.jobs['one']['result'], original)
        for _ in range(3):
            worker.receive(alert_request(req))
            self.assertEqual(worker.results.get_nowait(), ack)
        worker.receive(alert_request(req, event_id=8))
        self.assertTrue(worker.results.empty())
        self.assertEqual(len(calls), 2)

    def test_alert_failure_does_not_replace_recognition_or_retry_playback(self):
        calls = []
        def runner(command, req, cancel):
            calls.append(req['action'])
            if req['action'] == 'alert':
                raise OSError('speaker unavailable')
            return dict(status='ok', text='괜찮아요', classification='도움 불필요')
        worker = VoiceWorker([], runner, alert_command=['mpg123'])
        self.addCleanup(worker.close)
        req = request();worker.receive(req);worker.thread.join(1)
        original = worker.results.get_nowait()
        worker.receive(alert_request(req));worker.thread.join(1)
        self.assertEqual(worker.results.get_nowait()['status'], 'error')
        worker.receive(alert_request(req))
        self.assertEqual(calls, ['observe', 'alert'])
        self.assertEqual(worker.jobs['one']['result'], original)

    def test_completed_or_interrupted_alert_is_not_replayed_after_pi_restart(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)/'voice.json'
                req = request()
                first = VoiceWorker([], lambda *a: dict(status='ok', text='test',
                    classification='확인 필요'), state, alert_command=['play'])
                first.receive(req);first.thread.join(1)
                if interrupted:
                    first.jobs['one'].update(alert_event_id=7, alert_expires_at=req['expires_at'],
                                              alert_result=None)
                    first._save()  # crash after reserving playback, before recording its result
                else:
                    first.receive(alert_request(req));first.thread.join(1)
                second = VoiceWorker([], lambda *a: self.fail('must not replay'), state,
                                     alert_command=['play'])
                try:
                    second.receive(alert_request(req))
                    ack = second.results.get_nowait()
                    self.assertEqual((ack['action'], ack['event_id']), ('alert', 7))
                    if interrupted:
                        self.assertEqual((ack['status'], ack['stage']), ('error', 'restart'))
                finally:
                    first.close();second.close()

    def test_cancellation_blocks_delayed_alert_and_stops_active_playback(self):
        for active in (False, True):
            with self.subTest(active=active):
                entered = threading.Event()
                def runner(command, req, cancel):
                    if req['action'] == 'alert':
                        entered.set();cancel.wait(1)
                    return dict(status='ok', text='test', classification='확인 필요')
                worker = VoiceWorker([], runner, alert_command=['play'])
                req = request();worker.receive(req);worker.thread.join(1)
                original = worker.results.get_nowait()
                try:
                    if active:
                        worker.receive(alert_request(req));self.assertTrue(entered.wait(1))
                    worker.receive(dict(req, action='cancel'))
                    worker.receive(alert_request(req));worker.thread.join(1)
                    self.assertTrue(worker.jobs['one']['cancel'].is_set())
                    self.assertTrue(worker.results.empty())
                    self.assertEqual(worker.jobs['one']['result'], original)
                    if not active:
                        self.assertFalse(entered.is_set())
                finally:
                    worker.close()

    def test_alert_and_next_observation_share_single_audio_slot(self):
        entered, release = threading.Event(), threading.Event()
        def runner(command, req, cancel):
            if req['action'] == 'alert':
                entered.set();release.wait(1)
            return dict(status='ok', text='test', classification='확인 필요')
        worker = VoiceWorker([], runner, alert_command=['play'])
        self.addCleanup(worker.close)
        req = request();worker.receive(req);worker.thread.join(1)
        worker.receive(alert_request(req));self.assertTrue(entered.wait(1))
        worker.receive(request('two'))
        self.assertNotIn('two', worker.jobs)
        release.set();worker.thread.join(1)
        worker.receive(request('two'));worker.thread.join(1)
        self.assertIn('two', worker.jobs)

    def test_journal_failure_prevents_alert_playback(self):
        calls = []
        worker = VoiceWorker([], lambda command, req, cancel: calls.append(req['action']) or
                             dict(status='ok'), alert_command=['play'])
        self.addCleanup(worker.close)
        req = request();worker.receive(req);worker.thread.join(1);worker.results.get_nowait()
        with patch.object(worker, '_save', side_effect=OSError('read-only')):
            worker.receive(alert_request(req))
        self.assertEqual(calls, ['observe'])
        self.assertEqual(worker.results.get_nowait()['stage'], 'journal')

    def test_alert_process_accepts_player_exit_without_json_and_bounds_runtime(self):
        req = alert_request(request())
        cancelled = threading.Event();cancelled.set()
        with patch('my_bot_fall.voice_node.subprocess.Popen') as popen:
            self.assertEqual(run_process(['mpg123'], req, cancelled)['status'], 'error')
            popen.assert_not_called()
        result = run_process([sys.executable, '-c', 'print("player output")'], req, threading.Event())
        self.assertEqual((result['status'], result['stage']), ('ok', 'alert'))
        result = run_process([sys.executable, '-c', 'raise SystemExit(1)'], req, threading.Event())
        self.assertEqual((result['status'], result['stage']), ('error', 'alert'))
        started = time.monotonic()
        result = run_process([sys.executable, '-c', 'import time; time.sleep(30)'],
                             alert_request(request(ttl=0.2)), threading.Event())
        self.assertEqual((result['status'], result['stage']), ('error', 'alert'))
        self.assertLess(time.monotonic()-started, 5)


if __name__ == '__main__':
    unittest.main()
