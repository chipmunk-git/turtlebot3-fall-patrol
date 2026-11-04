"""In-process ASGI and in-memory database checks; no running server or robot."""

import asyncio
import importlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import config, models
from app.database import get_db
from app.models import ActivityLog, Event, EventStatus, RobotControl, utcnow


class BackendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.TemporaryDirectory()
        directory = Path(cls.files.name)
        # Importing main must not create runtime files in the working project.
        with patch.object(config, "SNAPSHOT_DIR", directory / "snapshots"), patch.object(
            config, "MAP_DIR", directory / "maps"
        ):
            cls.main = importlib.import_module("app.main")

    @classmethod
    def tearDownClass(cls):
        cls.files.cleanup()

    async def asyncSetUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        models.Base.metadata.create_all(self.engine)

        def database():
            with Session(self.engine, expire_on_commit=False) as db:
                yield db

        self.main.app.dependency_overrides[get_db] = database

    async def asyncTearDown(self):
        self.main.app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    async def request(self, method, url, data=None, form=False, content_type=None, extra_headers=None):
        parsed = urlsplit(url)
        headers = []
        body = b""
        if data is not None:
            if isinstance(data, bytes):
                body = data
                content_type = content_type or b"application/octet-stream"
            else:
                body = (urlencode(data) if form else json.dumps(data)).encode()
                content_type = b"application/x-www-form-urlencoded" if form else b"application/json"
            headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": parsed.path, "raw_path": parsed.path.encode(), "root_path": "",
            "query_string": parsed.query.encode(), "headers": headers,
            "server": ("test", 80), "client": ("test", 1234),
        }
        scope["headers"].extend(extra_headers or [])
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})
        messages = []

        async def send(message):
            messages.append(message)

        await asyncio.wait_for(self.main.app(scope, incoming.get, send), timeout=5)
        start = next(message for message in messages if message["type"] == "http.response.start")
        output = b"".join(message.get("body", b"") for message in messages
                          if message["type"] == "http.response.body")
        response_headers = dict(start["headers"])
        if b"application/json" in response_headers.get(b"content-type", b""):
            output = json.loads(output)
        return start["status"], output, response_headers

    def add_event(self, **fields):
        with Session(self.engine) as db:
            fields.setdefault("status", EventStatus.CONFIRMED)
            event = Event(x=0, y=0, **fields)
            db.add(event)
            db.commit()
            return event.id

    async def test_control_defaults_to_stop_and_does_not_change_robot_status(self):
        _, before, _ = await self.request("GET", "/status")
        code, control, _ = await self.request("GET", "/control")
        self.assertEqual((code, control["drive"]), (200, "stop"))
        await self.request("PUT", "/control", {"drive": "run"})
        _, after, _ = await self.request("GET", "/status")
        self.assertEqual(after, before)

    async def test_control_logs_changes_without_duplicating_unchanged_command(self):
        await self.request("PUT", "/control", {"drive": "run"})
        await self.request("PUT", "/control", {"drive": "run"})
        with Session(self.engine) as db:
            commands = list(db.scalars(select(ActivityLog).where(ActivityLog.kind == "drive_command")))
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0].detail, {"before": "stop", "after": "run"})

    async def test_invalid_drive_command_is_rejected(self):
        code, _, _ = await self.request("PUT", "/control", {"drive": "fast"})
        self.assertEqual(code, 422)
        _, control, _ = await self.request("GET", "/control")
        self.assertEqual(control["drive"], "stop")

    async def test_resolve_records_acknowledgement_and_preserves_voice_note(self):
        event_id = self.add_event(note="[voice:example] 도움 필요")
        code, event, _ = await self.request("PATCH", f"/events/{event_id}", {"status": "resolved"})
        self.assertEqual(code, 200)
        self.assertEqual(event["status"], "resolved")
        self.assertEqual(event["acknowledged_at"], event["resolved_at"])
        self.assertIsNotNone(event["acknowledged_at"])
        self.assertEqual(event["note"], "[voice:example] 도움 필요")
        _, events, _ = await self.request("GET", "/events")
        self.assertEqual([item["id"] for item in events], [event_id])

    async def test_resolve_preserves_previous_acknowledgement_time(self):
        from datetime import timedelta
        acknowledged = utcnow() - timedelta(minutes=1)
        event_id = self.add_event(acknowledged_at=acknowledged)
        _, event, _ = await self.request("PATCH", f"/events/{event_id}", {"status": "resolved"})
        self.assertNotEqual(event["acknowledged_at"], event["resolved_at"])
        with Session(self.engine) as db:
            self.assertEqual(db.get(Event, event_id).acknowledged_at, acknowledged)

    async def test_false_alarm_releases_ignore_zone_but_preserves_event(self):
        event_id = self.add_event()
        _, before, _ = await self.request("GET", "/ignore-zones/check?x=0&y=0")
        self.assertEqual(before, {"ignored": True, "event_id": event_id})
        await self.request("PATCH", f"/events/{event_id}", {"status": "false_alarm"})
        _, after, _ = await self.request("GET", "/ignore-zones/check?x=0&y=0")
        self.assertEqual(after, {"ignored": False, "event_id": None})
        _, event, _ = await self.request("GET", f"/events/{event_id}")
        self.assertEqual(event["status"], "false_alarm")

    async def test_ignore_zone_still_respects_distance_and_ttl(self):
        from datetime import timedelta
        event_id = self.add_event(created_at=utcnow() - timedelta(minutes=11))
        _, expired, _ = await self.request("GET", "/ignore-zones/check?x=0&y=0")
        self.assertFalse(expired["ignored"])
        with Session(self.engine) as db:
            db.get(Event, event_id).created_at = utcnow()
            db.commit()
        _, far, _ = await self.request("GET", "/ignore-zones/check?x=2&y=0")
        self.assertFalse(far["ignored"])

    async def test_original_event_creation_and_note_patch_remain_available(self):
        code, event, _ = await self.request("POST", "/events",
                                            {"status": "uncertain", "x": "1", "y": "2"}, form=True)
        self.assertEqual(code, 201)
        code, updated, _ = await self.request("PATCH", f"/events/{event['id']}", {"note": "음성 결과"})
        self.assertEqual((code, updated["status"], updated["note"]), (200, "uncertain", "음성 결과"))

    async def test_map_api_returns_origin_rotation(self):
        module = importlib.import_module("app.routers.map")
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "map.png"
            metadata = Path(temporary) / "map.yaml"
            image.write_bytes(b"test image")
            metadata.write_text("resolution: 0.05\norigin: [1, 2, 1.5707963267948966]\n")
            with patch.object(module, "MAP_IMAGE", image), patch.object(module, "MAP_YAML", metadata):
                code, result, _ = await self.request("GET", "/map")
            self.assertEqual(code, 200)
            self.assertEqual((result["origin_x"], result["origin_y"]), (1, 2))
            self.assertAlmostEqual(result["origin_yaw"], math.pi / 2)

    async def test_dashboard_revalidates_assets_without_changing_health_response(self):
        for url in ("/", "/app.js", "/style.css"):
            code, _, headers = await self.request("GET", url)
            self.assertEqual(code, 200)
            self.assertEqual(headers[b"cache-control"], b"no-cache")
        code, response, _ = await self.request("GET", "/health")
        self.assertEqual((code, response), (200, {"status": "ok"}))

    async def test_robot_log_is_stored_for_the_live_log(self):
        body = dict(source="patrol", level="warn", category="drive", message="정지: 라이다 수신 지연")
        code, _, _ = await self.request("POST", "/logs", body)
        self.assertEqual(code, 204)
        _, page, _ = await self.request("GET", "/logs")
        entry = page["entries"][-1]
        self.assertEqual((entry["kind"], entry["event_id"], entry["detail"]), ("robot_log", None, body))

    async def test_only_non_routine_robot_states_reach_the_live_log(self):
        for state in ("patrol", "idle", "patrol", "alert", "patrol"):
            await self.request("PUT", "/status", {"state": state})
        _, page, _ = await self.request("GET", "/logs")
        changes = [(e["detail"]["before"], e["detail"]["after"]) for e in page["entries"] if e["kind"] == "robot_state"]
        self.assertEqual(sorted(changes), [("alert", "patrol"), ("patrol", "alert")])

    async def test_robot_log_rejects_invalid_values(self):
        valid = dict(source="patrol", level="warn", category="drive", message="x")
        for change in (dict(level="fatal"), dict(category="other"), dict(message=""), dict(message="x"*1001),
                       dict(source=""), dict(source="x"*41)):
            with self.subTest(change=change):
                code, _, _ = await self.request("POST", "/logs", dict(valid, **change))
                self.assertEqual(code, 422)

    async def test_new_control_table_does_not_replace_existing_event_data(self):
        event_id = self.add_event(note="existing event")
        columns = [column["name"] for column in inspect(self.engine).get_columns("events")]
        RobotControl.__table__.drop(self.engine)
        with patch.object(self.main, "engine", self.engine):
            async with self.main.lifespan(self.main.app):
                pass
        self.assertIn("robot_control", inspect(self.engine).get_table_names())
        self.assertEqual(columns, [column["name"] for column in inspect(self.engine).get_columns("events")])
        with Session(self.engine) as db:
            self.assertEqual(db.get(Event, event_id).note, "existing event")

    async def test_event_alarm_waits_for_popup_before_ringing(self):
        event_id = self.add_event()
        _, alarms, _ = await self.request("GET", "/events/alarms")
        self.assertEqual(alarms["active_event_ids"], [])
        code, result, _ = await self.request("POST", f"/events/{event_id}/alarm/presented")
        self.assertEqual(code, 200)
        self.assertEqual(result, dict(event_id=event_id, presented=True, muted=False, active=True))
        _, alarms, _ = await self.request("GET", "/events/alarms")
        self.assertEqual(alarms["active_event_ids"], [event_id])

    async def test_only_red_and_yellow_events_can_activate_alarm(self):
        for status in EventStatus:
            with self.subTest(status=status):
                event_id = self.add_event(status=status)
                _, result, _ = await self.request("POST", f"/events/{event_id}/alarm/presented")
                self.assertEqual(result["active"], status in (EventStatus.CONFIRMED, EventStatus.UNCERTAIN))

    async def test_event_mute_keeps_status_voice_note_and_robot_control_unchanged(self):
        event_id = self.add_event(note="[voice:example] 도움 필요")
        _, before, _ = await self.request("GET", f"/events/{event_id}")
        _, robot, _ = await self.request("GET", "/status")
        _, drive, _ = await self.request("GET", "/control")
        await self.request("POST", f"/events/{event_id}/alarm/presented")
        _, result, _ = await self.request("POST", f"/events/{event_id}/alarm/mute")
        self.assertTrue(result["muted"])
        self.assertFalse(result["active"])
        _, after, _ = await self.request("GET", f"/events/{event_id}")
        self.assertEqual(after, before)
        self.assertEqual((await self.request("GET", "/status"))[1], robot)
        self.assertEqual((await self.request("GET", "/control"))[1], drive)

    async def test_mute_is_per_event_and_new_events_still_ring(self):
        first = self.add_event()
        second = self.add_event(status=EventStatus.UNCERTAIN)
        for event_id in (first, second):
            await self.request("POST", f"/events/{event_id}/alarm/presented")
        await self.request("POST", f"/events/{first}/alarm/mute")
        _, alarms, _ = await self.request("GET", "/events/alarms")
        self.assertEqual(alarms["active_event_ids"], [second])
        self.assertEqual(alarms["muted_event_ids"], [first])
        third = self.add_event()
        await self.request("POST", f"/events/{third}/alarm/presented")
        self.assertEqual((await self.request("GET", "/events/alarms"))[1]["active_event_ids"], [second, third])

    async def test_resolved_and_false_alarm_stop_without_changing_robot_alert(self):
        await self.request("PUT", "/status", {"state": "alert"})
        for status in ("resolved", "false_alarm"):
            with self.subTest(status=status):
                event_id = self.add_event()
                await self.request("POST", f"/events/{event_id}/alarm/presented")
                await self.request("PATCH", f"/events/{event_id}", {"status": status})
                self.assertEqual((await self.request("GET", "/events/alarms"))[1]["active_event_ids"], [])
        self.assertEqual((await self.request("GET", "/status"))[1]["state"], "alert")

    async def test_popup_retries_and_backend_restart_never_unmute_event(self):
        event_id = self.add_event()
        for _ in range(2):
            await self.request("POST", f"/events/{event_id}/alarm/presented")
        for _ in range(2):
            await self.request("POST", f"/events/{event_id}/alarm/mute")
        with patch.object(self.main, "engine", self.engine):
            async with self.main.lifespan(self.main.app):
                pass
        _, result, _ = await self.request("POST", f"/events/{event_id}/alarm/presented")
        self.assertTrue(result["muted"])
        self.assertFalse(result["active"])
        with Session(self.engine) as db:
            logs = list(db.scalars(select(ActivityLog).where(ActivityLog.event_id == event_id)))
            self.assertEqual(sorted(log.kind for log in logs), ["event_alarm_muted", "event_alarm_presented"])

    async def test_muting_before_popup_and_delayed_closed_popup_do_not_ring(self):
        event_id = self.add_event()
        await self.request("POST", f"/events/{event_id}/alarm/mute")
        _, result, _ = await self.request("POST", f"/events/{event_id}/alarm/presented")
        self.assertFalse(result["active"])
        other = self.add_event()
        await self.request("PATCH", f"/events/{other}", {"status": "resolved"})
        _, result, _ = await self.request("POST", f"/events/{other}/alarm/presented")
        self.assertFalse(result["presented"])
        self.assertFalse(result["active"])

    async def test_missing_event_alarm_actions_return_not_found(self):
        for action in ("presented", "mute"):
            code, _, _ = await self.request("POST", f"/events/999/alarm/{action}")
            self.assertEqual(code, 404)

    async def test_alarm_query_is_not_limited_to_first_dashboard_page(self):
        first = self.add_event()
        newer = self.add_event()
        await self.request("POST", f"/events/{first}/alarm/presented")
        self.assertEqual((await self.request("GET", "/events?limit=1"))[1][0]["id"], newer)
        self.assertEqual((await self.request("GET", "/events/alarms"))[1]["active_event_ids"], [first])


if __name__ == "__main__":
    unittest.main()
