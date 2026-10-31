import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EventStatus(str, enum.Enum):
    OBSERVING = "observing"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    FALSE_ALARM = "false_alarm"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class DriveCommand(str, enum.Enum):
    RUN = "run"
    STOP = "stop"


class RobotState(str, enum.Enum):
    IDLE = "idle"
    PATROL = "patrol"
    SUSPECT = "suspect"
    APPROACH = "approach"
    OBSERVE = "observe"
    ALERT = "alert"


class SnapshotKind(str, enum.Enum):
    DETECTED = "detected"
    OBSERVING = "observing"
    DECISION = "decision"


SNAPSHOT_PRIORITY = [SnapshotKind.DECISION, SnapshotKind.OBSERVING, SnapshotKind.DETECTED]


def enum_column(enum_class: type[enum.Enum]) -> Enum:
    return Enum(
        enum_class,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    x: Mapped[float | None]
    y: Mapped[float | None]
    confidence: Mapped[float | None]
    status: Mapped[EventStatus] = mapped_column(
        enum_column(EventStatus), default=EventStatus.OBSERVING, index=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    acknowledged_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    snapshots: Mapped[list["EventSnapshot"]] = relationship(
        back_populates="event", lazy="selectin", order_by="EventSnapshot.id"
    )

    @property
    def snapshot_url(self) -> str | None:
        by_kind = {snapshot.kind: snapshot for snapshot in self.snapshots}
        for kind in SNAPSHOT_PRIORITY:
            if kind in by_kind:
                return by_kind[kind].url
        return None


class EventSnapshot(Base):
    __tablename__ = "event_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    kind: Mapped[SnapshotKind] = mapped_column(enum_column(SnapshotKind))
    path: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    event: Mapped[Event] = relationship(back_populates="snapshots")

    @property
    def url(self) -> str:
        return f"/snapshots/{self.path}"


class RobotStatus(Base):
    __tablename__ = "robot_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[RobotState] = mapped_column(
        enum_column(RobotState), default=RobotState.IDLE
    )
    x: Mapped[float | None]
    y: Mapped[float | None]
    yaw: Mapped[float | None]
    current_waypoint: Mapped[int | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class RobotControl(Base):
    """웹에서 로봇에게 내리는 주행 명령. 로봇이 GET /control로 읽어 간다 (한 줄만 쓴다)."""

    __tablename__ = "robot_control"

    id: Mapped[int] = mapped_column(primary_key=True)
    drive: Mapped[DriveCommand] = mapped_column(enum_column(DriveCommand), default=DriveCommand.STOP)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    event_id: Mapped[int | None] = mapped_column(index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
