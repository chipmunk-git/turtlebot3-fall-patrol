from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.activity import record
from app.database import get_db
from app.models import RobotControl
from app.schemas import RobotControlRead, RobotControlUpdate

router = APIRouter(prefix="/control", tags=["control"])


def get_or_create_control(db: Session) -> RobotControl:
    control = db.get(RobotControl, 1)
    if control is None:
        control = RobotControl(id=1)
        db.add(control)
        db.commit()
    return control


@router.get("", response_model=RobotControlRead)
def get_control(db: Session = Depends(get_db)) -> RobotControl:
    return get_or_create_control(db)


@router.put("", response_model=RobotControlRead)
def update_control(data: RobotControlUpdate, db: Session = Depends(get_db)) -> RobotControl:
    control = get_or_create_control(db)
    if control.drive != data.drive:
        record(db, "drive_command", before=control.drive.value, after=data.drive.value)
        control.drive = data.drive
    db.commit()
    return control
