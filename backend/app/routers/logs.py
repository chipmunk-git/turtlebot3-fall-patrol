from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.activity import record
from app.database import get_db
from app.models import ActivityLog
from app.schemas import LogPage, RobotLogCreate

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("", response_model=LogPage)
def list_logs(
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    event_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> LogPage:
    stmt = select(ActivityLog).where(ActivityLog.id > after)
    if event_id is not None:
        stmt = stmt.where(ActivityLog.event_id == event_id)
    newest = db.scalars(stmt.order_by(ActivityLog.id.desc()).limit(limit)).all()
    last_id = db.scalar(select(func.max(ActivityLog.id))) or 0
    return LogPage(last_id=last_id, entries=list(reversed(newest)))


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
def add_log(data: RobotLogCreate, db: Session = Depends(get_db)) -> Response:
    record(db, "robot_log", None, **data.model_dump())
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
