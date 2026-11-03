"""Persist voice progress separately from event status and administrator notes."""

import re
import time
from datetime import timezone
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert

from app import config
from app.models import VoiceRecord, utcnow

PHASES = ("pending", "playback", "recording", "upload", "recognition")
TERMINAL = ("completed", "failed", "cancelled")


def ensure_voice(db, request_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
        raise HTTPException(400, "잘못된 음성 요청 ID입니다.")
    db.execute(insert(VoiceRecord).values(request_id=request_id).on_conflict_do_nothing())
    return db.get(VoiceRecord, request_id, populate_existing=True)


def bind_voice(db, request_id, event_id):
    record = ensure_voice(db, request_id)
    if record.event_id is not None and record.event_id != event_id:
        raise HTTPException(409, "이미 다른 사건에 연결된 음성 요청입니다.")
    db.execute(update(VoiceRecord).where(VoiceRecord.request_id == request_id).values(event_id=event_id))


def set_phase(db, request_id, phase, **fields):
    allowed = PHASES if phase in TERMINAL else PHASES[:PHASES.index(phase)+1]
    db.execute(update(VoiceRecord).where(VoiceRecord.request_id == request_id,
        VoiceRecord.phase.in_(allowed)).values(phase=phase, updated_at=utcnow(), **fields))
    db.commit()
    return db.get(VoiceRecord, request_id, populate_existing=True)


def audio_path(record):
    if not record.audio_filename or not re.fullmatch(r"[0-9a-f]{32}\.wav", record.audio_filename):
        return None
    path = config.VOICE_RECORDING_DIR / record.audio_filename
    # Do not serve symlinks or paths outside the recordings directory.
    if path.is_symlink() or not path.is_file():
        return None
    return path


def voice_view(record):
    if record is None:
        return None
    phase, message = record.phase, record.message
    deadline = record.expires_at or record.created_at.replace(tzinfo=timezone.utc).timestamp() + 120
    if phase not in TERMINAL and time.time() >= deadline:
        phase, message = "failed", "음성 처리 제한 시간을 초과했습니다."
    result = record.result or {}
    return dict(request_id=record.request_id, phase=phase, message=message,
                text=result.get("text"), classification=result.get("classification")
                or ("확인 필요" if phase in ("failed", "cancelled") else None),
                transcribe_seconds=result.get("transcribe_seconds"),
                audio_url=(f"/voice/{quote(record.request_id, safe='')}/audio"
                           if record.event_id is not None and audio_path(record) else None))
