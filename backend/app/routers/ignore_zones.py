import math

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.activity import record
from app.config import IGNORE_RADIUS_M, IGNORE_TTL
from app.database import get_db
from app.models import Event, EventStatus, utcnow
from app.schemas import IgnoreCheck

router = APIRouter(prefix="/ignore-zones", tags=["ignore-zones"])

ACTIVE_STATUSES = (
    EventStatus.OBSERVING,
    EventStatus.CONFIRMED,
    EventStatus.UNCERTAIN,
    EventStatus.ACKNOWLEDGED,
)


@router.get("/check", response_model=IgnoreCheck)
def check(x: float, y: float, db: Session = Depends(get_db)) -> IgnoreCheck:
    now = utcnow()
    stmt = (
        select(Event)
        .where(
            Event.x.is_not(None),
            Event.y.is_not(None),
            # 오탐은 사람이 확인한 결과라 무시하지 않고 다시 순찰 대상으로 둔다
            Event.status.in_(ACTIVE_STATUSES),
            Event.created_at >= now - IGNORE_TTL,
        )
        .order_by(Event.created_at.desc())
    )
    result = IgnoreCheck(ignored=False)
    for event in db.scalars(stmt):
        if math.hypot(event.x - x, event.y - y) <= IGNORE_RADIUS_M:
            result = IgnoreCheck(ignored=True, event_id=event.id)
            break
    record(db, "ignore_check", result.event_id, x=x, y=y, ignored=result.ignored)
    db.commit()
    return result
