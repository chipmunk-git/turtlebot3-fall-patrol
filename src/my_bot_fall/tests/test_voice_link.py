import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as O
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from my_bot_fall.voice_link import VoiceLink, append_note, valid_request
from test_patrol import fake_node, ns, person, pose

ns['String'] = lambda **kw: O(**kw)


class LinkTests(unittest.TestCase):
    def test_result_is_kept_before_event_id_exists(self):
        link = VoiceLink()
        link.start(100)
        result = dict(request_id=link.request['request_id'], status='ok',
                      text='괜찮아요', classification='도움 불필요')
        self.assertTrue(link.accept(result, 101))
        self.assertEqual(link.result['classification'], '도움 불필요')
        self.assertIsNone(link.message(102))
        self.assertFalse(link.accept(result, 103))

    def test_closed_and_previous_observations_cannot_accept_results(self):
        link = VoiceLink();link.start(100)
        old_id = link.request['request_id']
        link.end();link.start(101)
        self.assertFalse(link.accept(dict(request_id=old_id, status='ok', text='도와주세요',
                                         classification='도움 필요'), 102))

    def test_timeout_and_empty_transcript_do_not_mean_refusal(self):
        link = VoiceLink(timeout=5);link.start(100)
        self.assertEqual(link.message(106)['action'], 'cancel')
        self.assertEqual(link.result['classification'], '확인 필요')
        link.start(200)
        link.accept(dict(request_id=link.request['request_id'], status='ok', text='',
                         classification='도움 불필요'), 201)
        self.assertEqual(link.result['classification'], '확인 필요')

    def test_append_preserves_existing_note_and_deduplicates_retries(self):
        note = append_note('관리자 메모', '[voice:1]', '도움 필요')
        self.assertTrue(note.startswith('관리자 메모\n'))
        self.assertEqual(append_note(note, '[voice:1]', '도움 필요'), note)

    def test_expired_future_malformed_requests_are_rejected(self):
        for data in ([], {}, dict(request_id='x', action='observe', issued_at=90, expires_at=99),
                     dict(request_id='x', action='observe', issued_at=200, expires_at=210),
                     dict(request_id='x', action='observe', issued_at=100, expires_at=float('nan'))):
            self.assertFalse(valid_request(data, 100))


class ControllerVoiceTests(unittest.TestCase):
    def node(self):
        node = fake_node()
        node.voice_enabled = True
        node.voice = VoiceLink()
        node.clear_observation_image()
        node.voice_pub = O(publish=lambda message: None)
        return node

    def test_observation_starts_once_despite_repeated_detection(self):
        node = self.node()
        for _ in range(3):
            node.pose_callback_message(O(data=json.dumps(pose(person()))))
            request_id = node.voice.request['request_id']
            if hasattr(node, 'first_request'):
                self.assertEqual(request_id, node.first_request)
            node.first_request = request_id
        self.assertEqual(node.logic.phase, 'observe')
        self.assertIsNone(node.incident)

    def test_normal_recovery_cancels_voice_and_ignores_late_result(self):
        node = self.node();node.voice.start(time.time())
        request_id = node.voice.request['request_id']
        node.logic.phase = 'patrol';node.sync_voice()
        node.on_voice_result(O(data=json.dumps(dict(request_id=request_id, status='ok',
                                                    text='도와주세요', classification='도움 필요'))))
        self.assertIsNone(node.voice.result)
        self.assertIsNone(node.incident)
        self.assertEqual(node.voice.cancel['action'], 'cancel')

    def test_refusal_appends_to_same_event_without_changing_status_or_motion(self):
        node = self.node();node.voice.start(time.time());node.logic.phase = 'alert'
        key = node.voice.request['request_id']
        node.incident = dict(token='event-token', event_id=7, result='confirmed',
                             note_sent=True, voice_request_id=key)
        event = dict(status='confirmed', acknowledged_at=None, note='관리자 원래 메모')
        calls = []
        def http(method, path, data=None, *args):
            calls.append((method, path, data))
            if path == '/control':
                return {'drive': 'run'}
            if method == 'GET' and path == '/events/7':
                return dict(event)
            if method == 'PATCH':
                event.update(data)
            return {'status': 'ok'}
        node.http = http
        node.on_voice_result(O(data=json.dumps(dict(request_id=key, status='ok',
                                                    text='괜찮아요', classification='도움 불필요'))))
        node.web_cycle();node.web_cycle()
        patches = [data for method, path, data in calls if method == 'PATCH']
        self.assertEqual(len(patches), 1)
        self.assertEqual(set(patches[0]), {'note'})
        self.assertTrue(event['note'].startswith('관리자 원래 메모\n'))
        self.assertIn('도움 불필요', event['note'])
        self.assertEqual(event['status'], 'confirmed')
        self.assertEqual(node.logic.phase, 'alert')
        self.assertFalse(any(method == 'POST' for method, _, _ in calls))


if __name__ == '__main__':
    unittest.main()
