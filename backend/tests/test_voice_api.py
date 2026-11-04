"""Real ASGI uploads and WAVs; mock only the external whisper-cli process."""

import array
import asyncio
import importlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import test_backend_integration as support


def wav(samples=None, rate=16000):
    samples = [500, -500] * 800 if samples is None else samples
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        values = array.array("h", samples)
        if sys.byteorder != "little":
            values.byteswap()
        audio.writeframes(values.tobytes())
    return output.getvalue()


class VoiceApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        support.BackendIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        support.BackendIntegrationTests.tearDownClass()

    async def asyncSetUp(self):
        self.client = support.BackendIntegrationTests()
        await self.client.asyncSetUp()
        self.voice = importlib.import_module("app.routers.voice")
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.root = Path(self.files.name)
        self.expires = time.time()+60
        self.cli, self.model = self.root / "whisper-cli", self.root / "ggml-base-q8_0.bin"
        self.cli.touch(); self.model.touch()
        self.calls, self.wav_paths = [], []
        self.text = "도와주세요."
        from voice.stt import whisper_session
        def command(args, timeout):
            self.calls.append((args, timeout))
            return subprocess.CompletedProcess(args, 0, stdout=self.text, stderr="")
        validate = self.voice.validate_wav
        def check(path):
            self.wav_paths.append(path)
            return validate(path)
        for target, name, value in ((self.voice.config, "WHISPER_CLI", self.cli),
                                    (self.voice.config, "WHISPER_MODEL", self.model),
                                    (self.voice.config, "VOICE_RECORDING_DIR", self.root / "recordings"),
                                    (whisper_session, "run_command", command),
                                    (self.voice, "validate_wav", check)):
            mocked = patch.object(target, name, value)
            mocked.start(); self.addCleanup(mocked.stop)

    async def asyncTearDown(self):
        self.assertTrue(all(not path.exists() for path in self.wav_paths))
        await self.client.asyncTearDown()

    async def post(self, data=None, key="observation-one", expires=None, **options):
        boundary = "test-voice-upload"
        values = dict(request_id=key, expires_at=self.expires if expires is None else expires, **options)
        body = bytearray()
        for name, value in values.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
        body.extend(wav() if data is None else data)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        return await self.client.request("POST", "/voice/transcribe", bytes(body),
                    content_type=("multipart/form-data; boundary="+boundary).encode())

    async def test_upload_uses_existing_cpp_model_options_and_classifier(self):
        code, result, _ = await self.post()
        self.assertEqual(code, 200)
        self.assertEqual((result["status"], result["classification"], result["request_id"]),
                         ("ok", "도움 필요", "observation-one"))
        self.assertFalse(result["speech_validated"])
        args, timeout = self.calls[0]
        self.assertEqual(args[args.index("-m")+1], str(self.model.resolve()))
        self.assertEqual(args[args.index("-t")+1], "2")
        self.assertEqual(args[args.index("-l")+1], "ko")
        self.assertLessEqual(timeout, 40)
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session
        from app.models import Event, RobotControl, RobotStatus
        with Session(self.client.engine) as db:
            for model in (Event, RobotControl, RobotStatus):
                self.assertEqual(db.scalar(select(func.count()).select_from(model)), 0)

    async def test_repeated_upload_returns_cached_result_without_another_inference(self):
        expires = time.time()+60
        first = await self.post(expires=expires)
        second = await self.post(expires=expires)
        self.assertEqual(first[1], second[1])
        self.assertEqual(len(self.calls), 1)

    async def voice_logs(self):
        _, page, _ = await self.client.request("GET", "/logs")
        return [e["detail"] for e in page["entries"] if e["kind"] == "robot_log" and e["detail"]["category"] == "voice"]

    async def test_recognition_result_reaches_the_live_log_once(self):
        await self.post(key="logged-once")
        await self.post(key="logged-once")
        logs = await self.voice_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["level"], "warn")
        self.assertIn("분류: 도움 필요", logs[0]["message"])

    async def test_inference_failure_reaches_the_live_log(self):
        with patch.object(self.voice.config, "WHISPER_CLI", self.root / "missing-cli"):
            await self.post(key="logged-failure")
        logs = await self.voice_logs()
        self.assertEqual([log["level"] for log in logs], ["error"])
        self.assertIn("음성 처리 실패(preparation)", logs[0]["message"])

    async def test_log_commit_failure_preserves_the_recognition_result(self):
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session
        from app.models import ActivityLog, VoiceRecord
        original_commit = Session.commit

        def commit(db):
            if any(isinstance(item, ActivityLog) for item in db.new):
                db.flush()
                raise RuntimeError("activity log unavailable")
            return original_commit(db)

        with patch.object(Session, "commit", commit), self.assertLogs(self.voice.logger, level="WARNING"):
            code, result, _ = await self.post(key="log-write-failed")
        self.assertEqual((code, result["status"], result["classification"]), (200, "ok", "도움 필요"))
        with Session(self.client.engine) as db:
            record = db.get(VoiceRecord, "log-write-failed")
            self.assertEqual(record.phase, "completed")
            self.assertEqual(record.result, result)
            self.assertEqual(db.scalar(select(func.count()).select_from(ActivityLog)), 0)
        retry = await self.post(key="log-write-failed")
        self.assertEqual(retry[1], result)
        self.assertEqual(len(self.calls), 1)

    async def test_pi_failure_response_survives_log_storage_failure(self):
        with patch.object(self.voice, "record_activity", side_effect=RuntimeError("log unavailable")), \
                self.assertLogs(self.voice.logger, level="WARNING"):
            code, result, _ = await self.client.request("POST", "/voice/pi-failed/progress",
                                                       dict(phase="failed", expires_at=self.expires))
        self.assertEqual((code, result["phase"], result["classification"]), (200, "failed", "확인 필요"))

    async def test_voice_response_is_sent_before_slow_logging_finishes(self):
        entered, release = threading.Event(), threading.Event()
        response_sent = asyncio.Event()
        app = self.client.main.app

        def slow_log(*args):
            entered.set()
            release.wait(3)

        async def observed_app(scope, receive, send):
            async def observe(message):
                await send(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    response_sent.set()
            await app(scope, receive, observe)

        with patch.object(self.voice, "log_voice", slow_log), patch.object(self.client.main, "app", observed_app):
            task = asyncio.create_task(self.post(key="slow-log"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertTrue(response_sent.is_set())
                self.assertFalse(task.done())
                self.assertEqual((await self.client.request("GET", "/health"))[0], 200)
                self.assertEqual((await self.client.request("GET", "/control"))[0], 200)
            finally:
                release.set()
                code, result, _ = await task
        self.assertEqual((code, result["status"]), (200, "ok"))

    async def test_same_id_cannot_be_reused_for_different_audio_or_deadline(self):
        expires = time.time()+60
        await self.post(expires=expires)
        self.assertEqual((await self.post(wav([800]*1600), expires=expires))[0], 409)
        self.assertEqual((await self.post(expires=expires+1))[0], 409)
        self.assertEqual(len(self.calls), 1)

    async def test_busy_inference_does_not_block_health_or_drive_api(self):
        entered, release = threading.Event(), threading.Event()
        from voice.stt import whisper_session
        def slow(args, timeout):
            entered.set(); release.wait(3)
            return subprocess.CompletedProcess(args, 0, stdout="괜찮아요", stderr="")
        with patch.object(whisper_session, "run_command", slow):
            task = asyncio.create_task(self.post())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertEqual((await self.client.request("GET", "/health"))[0], 200)
                self.assertEqual((await self.client.request("GET", "/control"))[0], 200)
                self.assertEqual((await self.post(key="other"))[0], 503)
            finally:
                release.set()
                await task

    async def test_invalid_wavs_and_oversized_uploads_are_rejected(self):
        for data in (b"not wav", wav([], 16000), wav(rate=8000), wav()[:-100]):
            with self.subTest(length=len(data)):
                self.assertEqual((await self.post(data))[0], 400)
        self.assertEqual((await self.post(b"x"*(self.voice.MAX_AUDIO_BYTES+1)))[0], 413)
        self.assertEqual(self.calls, [])

    async def test_expired_future_and_invalid_requests_never_start_inference(self):
        for expiry in (time.time()-1, time.time()+121, float("nan"), float("inf")):
            self.assertEqual((await self.post(expires=expiry))[0], 400)
        for values in (dict(inference_timeout=0), dict(silence_rms=-1)):
            self.assertEqual((await self.post(**values))[0], 400)
        self.assertEqual(self.calls, [])

    async def test_digital_silence_requires_confirmation_without_running_whisper(self):
        code, result, _ = await self.post(wav([0]*1600))
        self.assertEqual((code, result["classification"], result["input_condition"]),
                         (200, "확인 필요", "silence"))
        self.assertEqual(self.calls, [])

    async def test_missing_model_and_inference_timeout_remain_errors(self):
        self.model.unlink()
        _, result, _ = await self.post()
        self.assertEqual((result["status"], result["stage"], result["classification"]),
                         ("error", "preparation", "확인 필요"))
        self.model.touch()
        from voice.stt import whisper_session
        with patch.object(whisper_session, "run_command", side_effect=subprocess.TimeoutExpired("whisper-cli", 1)):
            _, result, _ = await self.post(key="timeout")
        self.assertEqual((result["status"], result["stage"]), ("error", "recognition"))

    async def test_request_expiring_during_inference_cannot_return_a_success(self):
        clock = [time.time()]
        from voice.stt import whisper_session
        def command(args, timeout):
            clock[0] += 2
            return subprocess.CompletedProcess(args, 0, stdout="도와주세요", stderr="")
        with patch.object(self.voice, "time", SimpleNamespace(time=lambda: clock[0])), \
             patch.object(whisper_session, "run_command", command):
            self.assertEqual((await self.post(expires=clock[0]+1))[0], 408)

    async def test_server_uses_same_registered_expression_policy(self):
        examples = (
            ("사람 좀 불러 주세요!", "도움 필요"),        # 기존 도움 요청
            ("저 좀 일으켜 주세요!", "도움 필요"),      # 확장: 부축
            ("119에 신고해 주세요", "도움 필요"),      # 확장: 구조
            ("괜찮아요", "도움 불필요"),              # 기존 도움 거절
            ("저는 도움이 필요 없습니다", "도움 불필요"), # 확장: 명시적 거절
            ("저를 도와주실 필요 없어요", "도움 불필요"),
            ("괜찮아요. 그런데 못 일어나요", "확인 필요"),
            ("도움 필요 없어요. 하지만 도와주세요", "확인 필요"),
            ("괜찮지 않아요", "확인 필요"),
            ("못 일어나요", "확인 필요"),
            ("", "확인 필요"),
        )
        for i, (text, expected) in enumerate(examples):
            with self.subTest(text=text):
                self.text = text
                _, result, _ = await self.post(key=f"expanded-policy-{i}")
                self.assertEqual(result["classification"], expected)
                self.assertFalse(result["speech_validated"])

    async def progress(self, phase, key="observation-one"):
        return await self.client.request("POST", f"/voice/{key}/progress",
                                         dict(phase=phase, expires_at=self.expires))

    async def event(self, key="observation-one", status="confirmed"):
        code, result, _ = await self.client.request("POST", "/events",
                     dict(status=status, voice_request_id=key), form=True)
        self.assertEqual(code, 201)
        return result

    async def test_event_can_show_progress_then_result_without_changing_status_or_note(self):
        event = await self.event()
        _, record, _ = await self.client.request("GET", f"/voice/events/{event['id']}")
        self.assertEqual(record["phase"], "pending")
        for phase in ("playback", "recording", "upload"):
            await self.progress(phase)
            _, record, _ = await self.client.request("GET", f"/voice/events/{event['id']}")
            self.assertEqual(record["phase"], phase)
        await self.post()
        _, record, _ = await self.client.request("GET", f"/voice/events/{event['id']}")
        self.assertEqual((record["phase"], record["text"], record["classification"]),
                         ("completed", "도와주세요.", "도움 필요"))
        self.assertEqual((await self.client.request("GET", f"/events/{event['id']}"))[1], event)

    async def test_audio_before_event_is_kept_private_then_plays_after_binding(self):
        await self.post()
        self.assertEqual((await self.client.request("GET", "/voice/observation-one/audio"))[0], 404)
        event = await self.event()
        _, record, _ = await self.client.request("GET", f"/voice/events/{event['id']}")
        code, data, headers = await self.client.request("GET", record["audio_url"])
        self.assertEqual((code, data, headers[b"content-type"]), (200, wav(), b"audio/wav"))
        self.assertEqual(headers[b"cache-control"], b"no-store")
        code, data, _ = await self.client.request("GET", record["audio_url"],
                                                 extra_headers=[(b"range", b"bytes=0-15")])
        self.assertEqual((code, data), (206, wav()[:16]))

    async def test_recording_and_transcript_survive_server_restart_without_reinference(self):
        await self.post()
        event = await self.event()
        with patch.object(self.client.main, "engine", self.client.engine):
            async with self.client.main.lifespan(self.client.main.app):
                pass
        await self.post()
        self.assertEqual(len(self.calls), 1)
        _, record, _ = await self.client.request("GET", f"/voice/events/{event['id']}")
        self.assertTrue(record["audio_url"])
        self.assertEqual(len(list((self.root/"recordings").glob("*.wav"))), 1)

    async def test_delayed_progress_cannot_regress_completed_or_cancelled_exchange(self):
        await self.progress("recording")
        await self.progress("playback")
        self.assertEqual((await self.progress("upload"))[1]["phase"], "upload")
        await self.post()
        self.assertEqual((await self.progress("recording"))[1]["phase"], "completed")
        await self.progress("cancelled", key="other")
        self.assertEqual((await self.post(key="other"))[0], 409)

    async def test_cancel_during_inference_does_not_restore_a_completed_state(self):
        entered, release = threading.Event(), threading.Event()
        from voice.stt import whisper_session
        def slow(args, timeout):
            entered.set(); release.wait(3)
            return subprocess.CompletedProcess(args, 0, stdout="도와주세요", stderr="")
        event = await self.event()
        with patch.object(whisper_session, "run_command", slow):
            task = asyncio.create_task(self.post())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.progress("cancelled")
            finally:
                release.set()
                result = (await task)[1]
        self.assertEqual(result["status"], "error")
        self.assertEqual((await self.client.request("GET", f"/voice/events/{event['id']}"))[1]["phase"], "cancelled")

    async def test_one_observation_cannot_be_attached_to_another_event(self):
        await self.event()
        code, _, _ = await self.client.request("POST", "/events",
                  dict(status="uncertain", voice_request_id="observation-one"), form=True)
        self.assertEqual(code, 409)

    async def test_audio_path_traversal_and_unknown_events_do_not_expose_files(self):
        await self.post()
        event = await self.event()
        from sqlalchemy.orm import Session
        from app.models import VoiceRecord
        with Session(self.client.engine) as db:
            db.get(VoiceRecord, "observation-one").audio_filename = "../outside.wav"
            db.commit()
        self.assertEqual((await self.client.request("GET", "/voice/observation-one/audio"))[0], 404)
        self.assertEqual((await self.client.request("GET", "/voice/events/99999"))[0], 404)

    async def test_new_voice_table_does_not_replace_existing_event_data(self):
        from sqlalchemy import inspect
        from app.models import VoiceRecord
        event = await self.event()
        columns = [c["name"] for c in inspect(self.client.engine).get_columns("events")]
        VoiceRecord.__table__.drop(self.client.engine)
        with patch.object(self.client.main, "engine", self.client.engine):
            async with self.client.main.lifespan(self.client.main.app):
                pass
        self.assertEqual([c["name"] for c in inspect(self.client.engine).get_columns("events")], columns)
        self.assertEqual((await self.client.request("GET", f"/events/{event['id']}"))[1], event)


if __name__ == "__main__":
    unittest.main()
