from datetime import timedelta
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
STATIC_DIR = BASE_DIR / "static"
MAP_DIR = BASE_DIR / "maps"
MAP_IMAGE = MAP_DIR / "map.png"
MAP_YAML = MAP_DIR / "map.yaml"

DATABASE_URL = f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"

# Server-native whisper.cpp executable and the existing multilingual quantized model.
WHISPER_CLI = Path(os.environ.get("FALL_WHISPER_CLI", "~/whisper.cpp/build/bin/whisper-cli")).expanduser()
WHISPER_MODEL = Path(os.environ.get("FALL_WHISPER_MODEL", "~/whisper.cpp/models/ggml-base-q8_0.bin")).expanduser()
VOICE_RECORDING_DIR = BASE_DIR.parent / "voice" / "recordings"

IGNORE_RADIUS_M = 1.0
IGNORE_TTL = timedelta(minutes=10)
