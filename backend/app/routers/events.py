import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.activity import record
from app.config import SNAPSHOT_DIR
from app.database import get_db
from app.models import Event, EventSnapshot, EventStatus, SnapshotKind, utcnow
from app.schemas import EventRead, EventUpdate

router = APIRouter(prefix="/events", tags=["events"])

SNAPSHOT_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}


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
    db: Session = Depends(get_db),
) -> Event:
    event = Event(x=x, y=y, confidence=confidence)
    apply_status(event, status)
    for kind, upload in ((SnapshotKind.DETECTED, detected_snapshot), (SnapshotKind.OBSERVING, observing_snapshot)):
        if has_file(upload):
            await attach_snapshot(event, kind, upload)
    db.add(event)
    db.flush()
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
