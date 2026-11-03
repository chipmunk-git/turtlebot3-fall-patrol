import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.activity import record
from app.config import SNAPSHOT_DIR
from app.database import get_db
from app.models import ActivityLog, Event, EventSnapshot, EventStatus, SnapshotKind, utcnow
from app.schemas import AlarmState, EventAlarmRead, EventRead, EventUpdate
from app.voice_records import bind_voice

router = APIRouter(prefix="/events", tags=["events"])

SNAPSHOT_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}
ALARM_STATUSES = (EventStatus.CONFIRMED, EventStatus.UNCERTAIN)
ALARM_PRESENTED = "event_alarm_presented"
ALARM_MUTED = "event_alarm_muted"


def alarm_recorded(db: Session, event_id: int, kind: str) -> bool:
    return db.scalar(select(ActivityLog.id).where(
        ActivityLog.event_id == event_id, ActivityLog.kind == kind
    ).limit(1)) is not None


def alarm_state(db: Session, event: Event) -> dict:
    presented = alarm_recorded(db, event.id, ALARM_PRESENTED)
    muted = alarm_recorded(db, event.id, ALARM_MUTED)
    return dict(event_id=event.id, presented=presented, muted=muted,
                active=event.status in ALARM_STATUSES and presented and not muted)


def apply_status(event: Event, status: EventStatus) -> None:
    event.status = status
    if status == EventStatus.ACKNOWLEDGED and event.acknowledged_at is None:
        event.acknowledged_at = utcnow()
    if status == EventStatus.RESOLVED and event.resolved_at is None:
        event.resolved_at = utcnow()
        if event.acknowledged_at is None:
            event.acknowledged_at = event.resolved_at


def has_file(upload: UploadFile | None) -> bool:
    return upload is not None and bool(upload.filename)


async def attach_snapshot(event: Event, kind: SnapshotKind, upload: UploadFile) -> None:
    extension = SNAPSHOT_EXTENSIONS.get(upload.content_type or "")
    if extension is None:
        raise HTTPException(status_code=400, detail="snapshot must be jpeg or png")
    filename = f"{uuid.uuid4().hex}{extension}"
    (SNAPSHOT_DIR / filename).write_bytes(await upload.read())
    event.snapshots.append(EventSnapshot(kind=kind, path=filename))


def get_event_or_404(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return event


@router.post("", response_model=EventRead, status_code=201)
async def create_event(
    x: float | None = Form(None),
    y: float | None = Form(None),
    confidence: float | None = Form(None),
    status: EventStatus = Form(EventStatus.OBSERVING),
    detected_snapshot: UploadFile | None = File(None),
    observing_snapshot: UploadFile | None = File(None),
    voice_request_id: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Event:
    event = Event(x=x, y=y, confidence=confidence)
    apply_status(event, status)
    for kind, upload in ((SnapshotKind.DETECTED, detected_snapshot), (SnapshotKind.OBSERVING, observing_snapshot)):
        if has_file(upload):
            await attach_snapshot(event, kind, upload)
    db.add(event)
    db.flush()
    if voice_request_id:
        bind_voice(db, voice_request_id, event.id)
    record(
        db,
        "event_created",
        event.id,
        status=event.status.value,
        x=x,
        y=y,
        confidence=confidence,
        snapshots=[snapshot.kind.value for snapshot in event.snapshots],
    )
    db.commit()
    return event


@router.get("", response_model=list[EventRead])
def list_events(
    status: list[EventStatus] | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Event]:
    stmt = select(Event).order_by(Event.created_at.desc(), Event.id.desc())
    if status:
        stmt = stmt.where(Event.status.in_(status))
    return list(db.scalars(stmt.limit(limit).offset(offset)))


@router.get("/alarms", response_model=AlarmState)
def list_alarms(db: Session = Depends(get_db)) -> dict:
    # Query all pending events, independently of the dashboard's list limit.
    pending = select(Event.id).where(Event.status.in_(ALARM_STATUSES))
    rows = db.execute(select(ActivityLog.event_id, ActivityLog.kind).where(
        ActivityLog.event_id.in_(pending),
        ActivityLog.kind.in_((ALARM_PRESENTED, ALARM_MUTED)),
    ))
    presented, muted = set(), set()
    for event_id, kind in rows:
        (presented if kind == ALARM_PRESENTED else muted).add(event_id)
    return dict(active_event_ids=sorted(presented - muted),
                presented_event_ids=sorted(presented), muted_event_ids=sorted(muted))


@router.post("/{event_id}/alarm/presented", response_model=EventAlarmRead)
def present_alarm(event_id: int, db: Session = Depends(get_db)) -> dict:
    event = get_event_or_404(db, event_id)
    if event.status in ALARM_STATUSES and not alarm_recorded(db, event_id, ALARM_PRESENTED):
        record(db, ALARM_PRESENTED, event_id)
        db.commit()
    return alarm_state(db, event)


@router.post("/{event_id}/alarm/mute", response_model=EventAlarmRead)
def mute_alarm(event_id: int, db: Session = Depends(get_db)) -> dict:
    event = get_event_or_404(db, event_id)
    if event.status in ALARM_STATUSES and not alarm_recorded(db, event_id, ALARM_MUTED):
        record(db, ALARM_MUTED, event_id)
        db.commit()
    return alarm_state(db, event)


@router.get("/{event_id}", response_model=EventRead)
def get_event(event_id: int, db: Session = Depends(get_db)) -> Event:
    return get_event_or_404(db, event_id)


@router.post("/{event_id}/snapshots", response_model=EventRead, status_code=201)
async def add_snapshot(
    event_id: int,
    kind: SnapshotKind = Form(...),
    snapshot: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Event:
    event = get_event_or_404(db, event_id)
    await attach_snapshot(event, kind, snapshot)
    event.updated_at = utcnow()
    record(db, "event_snapshot", event.id, kind=kind.value)
    db.commit()
    return event


@router.patch("/{event_id}", response_model=EventRead)
def update_event(event_id: int, data: EventUpdate, db: Session = Depends(get_db)) -> Event:
    event = get_event_or_404(db, event_id)
    fields = data.model_dump(exclude_unset=True)
    previous = event.status
    if fields.get("status") is not None:
        apply_status(event, fields["status"])
    if "note" in fields:
        event.note = fields["note"]
    event.updated_at = utcnow()
    if event.status != previous:
        record(db, "event_status", event.id, before=previous.value, after=event.status.value)
    if "note" in fields:
        record(db, "event_note", event.id, note=event.note)
    db.commit()
    return event
