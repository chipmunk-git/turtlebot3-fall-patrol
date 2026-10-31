from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
STATIC_DIR = BASE_DIR / "static"
MAP_DIR = BASE_DIR / "maps"
MAP_IMAGE = MAP_DIR / "map.png"
MAP_YAML = MAP_DIR / "map.yaml"

DATABASE_URL = f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"

IGNORE_RADIUS_M = 1.0
IGNORE_TTL = timedelta(minutes=10)
