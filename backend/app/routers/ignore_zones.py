import math

from fastapi import APIRouter, Depends
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.activity import record
from app.config import FALSE_ALARM_TTL, IGNORE_RADIUS_M, IGNORE_TTL
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
            or_(
                and_(Event.status.in_(ACTIVE_STATUSES), Event.created_at >= now - IGNORE_TTL),
                and_(
                    Event.status == EventStatus.FALSE_ALARM,
                    Event.created_at >= now - FALSE_ALARM_TTL,
                ),
            ),
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
