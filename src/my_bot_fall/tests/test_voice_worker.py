from pathlib import Path
import queue
import sys
import threading
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from my_bot_fall.voice_node import VoiceWorker, run_process


def request(key='one', ttl=10):
    now = time.time()
    return dict(request_id=key, action='observe', issued_at=now, expires_at=now+ttl)


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


if __name__ == '__main__':
    unittest.main()
