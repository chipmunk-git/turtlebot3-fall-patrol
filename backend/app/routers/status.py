from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.activity import record
from app.database import get_db
from app.models import RobotStatus, utcnow
from app.schemas import RobotStatusRead, RobotStatusUpdate

router = APIRouter(prefix="/status", tags=["status"])


def get_or_create_status(db: Session) -> RobotStatus:
    status = db.get(RobotStatus, 1)
    if status is None:
        status = RobotStatus(id=1)
        db.add(status)
        db.commit()
    return status


@router.get("", response_model=RobotStatusRead)
def get_status(db: Session = Depends(get_db)) -> RobotStatus:
    return get_or_create_status(db)


@router.put("", response_model=RobotStatusRead)
def update_status(data: RobotStatusUpdate, db: Session = Depends(get_db)) -> RobotStatus:
    status = get_or_create_status(db)
    previous = status.state
    for field, value in data.model_dump().items():
        setattr(status, field, value)
    status.updated_at = utcnow()
    if status.state != previous:
        record(db, "robot_state", before=previous.value, after=status.state.value, x=status.x, y=status.y)
    db.commit()
    return status
