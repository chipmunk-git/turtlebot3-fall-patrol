import math
import random
import shutil
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.config import MAP_DIR, MAP_IMAGE, SNAPSHOT_DIR
from app.database import SessionLocal, engine
from app.models import ActivityLog, Base, Event, EventSnapshot, EventStatus, SnapshotKind, utcnow
from scripts.fake_world import write_map
from scripts.patrol import Patrol, load_patrol
from scripts.png_utils import make_snapshot

EVENT_COUNT = 20
NOTES = ["보호자 인계", "간호사 확인 완료", "외상 없음, 침대로 이동", "무릎 찰과상 처치", None]
FALSE_ALARM_NOTES = ["쉬고 있던 분", "물건 줍는 중", "바닥의 옷 더미", None]
OLD_STATUSES = [
    EventStatus.RESOLVED,
    EventStatus.RESOLVED,
    EventStatus.RESOLVED,
    EventStatus.FALSE_ALARM,
    EventStatus.FALSE_ALARM,
    EventStatus.ACKNOWLEDGED,
]
RECENT = [
    (EventStatus.CONFIRMED, timedelta(minutes=2)),
    (EventStatus.UNCERTAIN, timedelta(minutes=5)),
    (EventStatus.OBSERVING, timedelta(seconds=20)),
]
APPROACH_TIME = timedelta(seconds=15)
OBSERVE_TIME = timedelta(seconds=20)

LogDraft = tuple[datetime, str, bool, dict[str, Any]]


def save_snapshot(seed: int, kind: SnapshotKind, at: datetime) -> EventSnapshot:
    filename = f"{uuid.uuid4().hex}.png"
    (SNAPSHOT_DIR / filename).write_bytes(make_snapshot(seed, kind.value))
    return EventSnapshot(kind=kind, path=filename, created_at=at)


def random_position(rng: random.Random, patrol: Patrol) -> tuple[float, float]:
    while True:
        wx, wy = rng.choice(patrol.waypoints)
        x, y = wx + rng.uniform(-0.15, 0.15), wy + rng.uniform(-0.15, 0.15)
        if all(math.hypot(x - sx, y - sy) > 1.2 for sx, sy in patrol.person_spots):
            return round(x, 2), round(y, 2)


def status_path(rng: random.Random, status: EventStatus) -> list[EventStatus]:
    if status == EventStatus.OBSERVING:
        return []
    if status in (EventStatus.CONFIRMED, EventStatus.UNCERTAIN, EventStatus.FALSE_ALARM):
        return [status]
    first = [EventStatus.UNCERTAIN, EventStatus.CONFIRMED] if rng.random() < 0.3 else [EventStatus.CONFIRMED]
    tail = [EventStatus.ACKNOWLEDGED] + ([EventStatus.RESOLVED] if status == EventStatus.RESOLVED else [])
    return first + tail


def build_event(
    rng: random.Random, patrol: Patrol, status: EventStatus, age: timedelta
) -> tuple[Event, list[LogDraft]]:
    created_at = utcnow() - age
    detected_at = created_at - APPROACH_TIME
    decided_at = created_at + OBSERVE_TIME
    x, y = random_position(rng, patrol)
    robot = {"x": round(x - 0.6, 2), "y": y}
    seed = rng.randrange(1 << 30)

    event = Event(
        created_at=created_at,
        updated_at=created_at,
        x=x,
        y=y,
        confidence=round(rng.uniform(0.45, 0.95), 2),
        status=EventStatus.OBSERVING,
        snapshots=[
            save_snapshot(seed, SnapshotKind.DETECTED, detected_at),
            save_snapshot(seed, SnapshotKind.OBSERVING, created_at),
        ],
    )
    logs: list[LogDraft] = [
        (detected_at, "robot_state", False, {"before": "patrol", "after": "suspect", **robot}),
        (detected_at + timedelta(seconds=2), "ignore_check", False, {"x": x, "y": y, "ignored": False}),
        (detected_at + timedelta(seconds=3), "robot_state", False, {"before": "suspect", "after": "approach", **robot}),
        (created_at, "robot_state", False, {"before": "approach", "after": "observe", **robot}),
        (
            created_at,
            "event_created",
            True,
            {"status": "observing", "x": x, "y": y, "confidence": event.confidence, "snapshots": ["detected", "observing"]},
        ),
    ]

    path = status_path(rng, status)
    if path:
        event.snapshots.append(save_snapshot(seed, SnapshotKind.DECISION, decided_at))
        after = "alert" if path[0] == EventStatus.CONFIRMED else "patrol"
        logs += [
            (decided_at, "event_snapshot", True, {"kind": "decision"}),
            (decided_at, "robot_state", False, {"before": "observe", "after": after, **robot}),
        ]

    at = decided_at
    for step in path:
        if step == EventStatus.ACKNOWLEDGED:
            at += timedelta(seconds=rng.randint(20, 240))
            event.acknowledged_at = at
        elif step == EventStatus.RESOLVED:
            at += timedelta(minutes=rng.randint(3, 20))
            event.resolved_at = at
        elif step != path[0]:
            at += timedelta(seconds=rng.randint(30, 90))
        logs.append((at, "event_status", True, {"before": event.status.value, "after": step.value}))
        event.status = step
        event.updated_at = at

    note_pool = FALSE_ALARM_NOTES if status == EventStatus.FALSE_ALARM else NOTES
    if status in (EventStatus.RESOLVED, EventStatus.FALSE_ALARM):
        event.note = rng.choice(note_pool)
        if event.note:
            at += timedelta(seconds=rng.randint(10, 60))
            logs.append((at, "event_note", True, {"note": event.note}))
            event.updated_at = at
    return event, logs


def main() -> None:
    rng = random.Random()
    shutil.rmtree(SNAPSHOT_DIR, ignore_errors=True)
    SNAPSHOT_DIR.mkdir(parents=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    if not MAP_IMAGE.exists():
        write_map(MAP_DIR)
    patrol = load_patrol()

    built = [build_event(rng, patrol, status, age) for status, age in RECENT]
    for _ in range(EVENT_COUNT - len(RECENT)):
        age = timedelta(hours=rng.uniform(1, 72))
        built.append(build_event(rng, patrol, rng.choice(OLD_STATUSES), age))

    with SessionLocal() as db:
        db.add_all(event for event, _ in built)
        db.flush()
        drafts = [
            (at, kind, event.id if linked else None, detail)
            for event, logs in built
            for at, kind, linked, detail in logs
        ]
        drafts.sort(key=lambda draft: draft[0])
        db.add_all(
            ActivityLog(created_at=at, kind=kind, event_id=event_id, detail=detail)
            for at, kind, event_id, detail in drafts
        )
        db.commit()
    print(f"seeded {len(built)} events, {len(drafts)} logs")


if __name__ == "__main__":
    main()
