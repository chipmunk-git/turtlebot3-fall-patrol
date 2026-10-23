from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer

from app.models import EventStatus, RobotState, SnapshotKind


def to_utc_iso(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(to_utc_iso, return_type=str)]


class SnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: SnapshotKind
    url: str
    created_at: UtcDatetime


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: UtcDatetime
    updated_at: UtcDatetime
    x: float | None
    y: float | None
    confidence: float | None
    status: EventStatus
    snapshot_url: str | None
    snapshots: list[SnapshotRead]
    note: str | None
    acknowledged_at: UtcDatetime | None
    resolved_at: UtcDatetime | None


class EventUpdate(BaseModel):
    status: EventStatus | None = None
    note: str | None = None


class IgnoreCheck(BaseModel):
    ignored: bool
    event_id: int | None = None


class LogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: UtcDatetime
    kind: str
    event_id: int | None
    detail: dict[str, Any]


class LogPage(BaseModel):
    last_id: int
    entries: list[LogEntry]


class MapInfo(BaseModel):
    image_url: str
    resolution: float
    origin_x: float
    origin_y: float


class RobotStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: RobotState
    x: float | None
    y: float | None
    yaw: float | None
    current_waypoint: int | None
    updated_at: UtcDatetime


class RobotStatusUpdate(BaseModel):
    state: RobotState
    x: float | None = None
    y: float | None = None
    yaw: float | None = None
    current_waypoint: int | None = None
