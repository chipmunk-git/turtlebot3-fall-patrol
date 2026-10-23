from typing import Any

from sqlalchemy.orm import Session

from app.models import ActivityLog


def record(db: Session, kind: str, event_id: int | None = None, /, **detail: Any) -> None:
    db.add(ActivityLog(kind=kind, event_id=event_id, detail=detail))
