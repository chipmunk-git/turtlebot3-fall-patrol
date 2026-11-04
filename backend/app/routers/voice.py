"""Voice progress, bounded inference and event-linked recording playback."""

import hashlib
import logging
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import wave
import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.database import get_db
from app.models import Event, VoiceRecord
from app.activity import record as record_activity
from app.voice_records import ensure_voice, set_phase, voice_view, audio_path, TERMINAL

# Support both repository-root and backend/ working directories without installation.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from voice.stt.whisper_session import recognize_file

router = APIRouter(prefix="/voice", tags=["voice"])
logger = logging.getLogger(__name__)


def log_voice(bind, event_id, level, message):
    """Write after the response, in a session separate from the voice result."""
    try:
        with Session(bind) as db:
            record_activity(db, "robot_log", event_id, source="server", level=level,
                            category="voice", message=message[:1000])
            db.commit()
    except Exception:
        logger.warning("음성 활동 로그를 저장하지 못했습니다.")


MAX_AUDIO_BYTES = 192000
INFERENCE_LOCK = threading.Lock()


class Progress(BaseModel):
    phase: Literal["playback", "recording", "upload", "failed", "cancelled"]
    expires_at: float


@router.post("/{request_id}/progress")
def progress(request_id: str, value: Progress, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not math.isfinite(value.expires_at) or not 0 < value.expires_at-time.time() <= 120:
        raise HTTPException(400, "잘못되었거나 만료된 음성 요청입니다.")
    record = ensure_voice(db, request_id)
    if record.expires_at is not None and record.expires_at != value.expires_at:
        raise HTTPException(409, "음성 요청의 만료 시각이 다릅니다.")
    message = "Pi 음성 처리가 실패했습니다." if value.phase == "failed" else None
    previous = record.phase
    record = set_phase(db, request_id, value.phase, expires_at=value.expires_at, message=message)
    if value.phase == "failed" and previous != "failed" and record.phase == "failed":
        background_tasks.add_task(log_voice, db.get_bind(), record.event_id,
                                  "error", "음성 처리 실패: Pi에서 처리하지 못했습니다.")
    return voice_view(record)


@router.get("/events/{event_id}")
def event_voice(event_id: int, db: Session = Depends(get_db)):
    if db.get(Event, event_id) is None:
        raise HTTPException(404, "사건을 찾을 수 없습니다.")
    record = db.scalar(select(VoiceRecord).where(VoiceRecord.event_id == event_id)
                       .order_by(VoiceRecord.created_at.desc()).limit(1))
    return voice_view(record)


@router.get("/{request_id}/audio")
def recording(request_id: str, db: Session = Depends(get_db)):
    record = db.get(VoiceRecord, request_id)
    path = audio_path(record) if record else None
    if (not record or record.event_id is None or db.get(Event, record.event_id) is None or not path):
        raise HTTPException(404, "사건에 연결된 녹음을 찾을 수 없습니다.")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


def validate_wav(path):
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        if ((audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2)
                or not 0 < frames <= 5 * 16000 or len(audio.readframes(frames)) != frames * 2):
            raise ValueError("invalid WAV")


@router.post("/transcribe")
def transcribe(background_tasks: BackgroundTasks, audio: UploadFile = File(...),
               request_id: str = Form(..., min_length=1, max_length=100),
               expires_at: float = Form(...), inference_timeout: float = Form(40.0),
               silence_rms: float = Form(0.0), db: Session = Depends(get_db)) -> dict:
    # A synchronous route runs inference in FastAPI's thread pool, not its event loop.
    now = time.time()
    if (not math.isfinite(expires_at) or not 0 < expires_at - now <= 120
            or not math.isfinite(inference_timeout) or inference_timeout <= 0
            or not math.isfinite(silence_rms) or silence_rms < 0):
        raise HTTPException(400, "만료되었거나 잘못된 음성 요청입니다.")
    data = audio.file.read(MAX_AUDIO_BYTES + 1)
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "5초 이내의 WAV만 받을 수 있습니다.")
    digest = hashlib.sha256(data).hexdigest()
    if not INFERENCE_LOCK.acquire(blocking=False):
        raise HTTPException(503, "다른 음성 요청을 처리 중입니다.")
    try:
        record = ensure_voice(db, request_id)
        if ((record.digest and record.digest != digest)
                or (record.expires_at is not None and record.expires_at != expires_at)):
            raise HTTPException(409, "같은 요청 ID에 다른 녹음·만료 시각을 사용할 수 없습니다.")
        if record.result:
            return dict(record.result)
        if record.phase in TERMINAL:
            raise HTTPException(409, "종료된 음성 요청입니다.")
        with tempfile.TemporaryDirectory(prefix="fall_stt_") as directory:
            path = Path(directory) / "speech.wav"
            path.write_bytes(data)
            try:
                validate_wav(path)
            except (ValueError, EOFError, wave.Error):
                raise HTTPException(400, "16 kHz·모노·16비트 PCM, 5초 이내 WAV여야 합니다.")
            config.VOICE_RECORDING_DIR.mkdir(parents=True, exist_ok=True)
            saved_path = audio_path(record)
            reuse_audio = saved_path is not None
            filename = record.audio_filename if saved_path else uuid.uuid4().hex + ".wav"
            saved_path = saved_path or config.VOICE_RECORDING_DIR / filename
            created = False
            try:
                if not reuse_audio:
                    with saved_path.open("xb") as output:
                        created = True
                        output.write(data)
                record = set_phase(db, request_id, "recognition", digest=digest,
                                   expires_at=expires_at, audio_filename=filename)
                if record.phase in TERMINAL:
                    raise HTTPException(409, "종료된 음성 요청입니다.")
            except Exception:
                if created:
                    saved_path.unlink(missing_ok=True)
                raise
            try:
                timeout = min(40.0, inference_timeout, expires_at - time.time())
                if timeout <= 0:
                    raise subprocess.TimeoutExpired("whisper-cli", 0)
                result = recognize_file(path, cli=config.WHISPER_CLI, model=config.WHISPER_MODEL,
                                        inference_timeout=timeout, silence_rms=silence_rms)
            except FileNotFoundError:
                result = dict(status="error", stage="preparation", message="서버 Whisper 실행 파일·모델 경로를 확인하세요.")
            except subprocess.TimeoutExpired:
                result = dict(status="error", stage="recognition", message="서버 음성 인식 제한 시간을 초과했습니다.")
            except Exception:
                result = dict(status="error", stage="recognition", message="서버 음성 인식에 실패했습니다.")
        if time.time() >= expires_at:
            expired = set_phase(db, request_id, "failed", message="음성 요청이 처리 중 만료되었습니다.")
            if expired.phase == "failed":
                background_tasks.add_task(log_voice, db.get_bind(), expired.event_id,
                                          "error", "음성 처리 실패: 처리 중 요청이 만료되었습니다.")
            return JSONResponse(status_code=408, content={"detail": "음성 요청이 처리 중 만료되었습니다."},
                                background=background_tasks)
        if result["status"] == "error":
            result["classification"] = "확인 필요"
        result = dict(result, request_id=request_id)
        record = set_phase(db, request_id, "completed" if result["status"] == "ok" else "failed",
                           result=result, message=result.get("message"))
        if record.phase == "completed":
            classification = result.get("classification", "확인 필요")
            background_tasks.add_task(log_voice, db.get_bind(), record.event_id,
                                      "warn" if classification == "도움 필요" else "info",
                                      f'음성 인식: "{result.get("text", "")}" / 분류: {classification}')
        elif record.phase == "failed":
            background_tasks.add_task(log_voice, db.get_bind(), record.event_id, "error",
                                      f'음성 처리 실패({result.get("stage", "-")}): {result.get("message", "")}')
        if record.phase == "cancelled":
            return dict(status="error", request_id=request_id, stage="cancelled",
                        classification="확인 필요", message="취소된 음성 요청입니다.")
        return result
    finally:
        INFERENCE_LOCK.release()
