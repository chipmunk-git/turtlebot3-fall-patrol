import json
from dataclasses import asdict, dataclass
from pathlib import Path

PATROL_FILE = Path(__file__).resolve().parent.parent / "maps" / "patrol.json"

Point = tuple[float, float]


@dataclass
class Patrol:
    waypoints: list[Point]
    person_spots: list[Point]
    detect_range: float
    approach_distance: float


def load_patrol(path: Path = PATROL_FILE) -> Patrol:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Patrol(
        waypoints=[(x, y) for x, y in data["waypoints"]],
        person_spots=[(x, y) for x, y in data["person_spots"]],
        detect_range=data["detect_range"],
        approach_distance=data["approach_distance"],
    )


def save_patrol(patrol: Patrol, path: Path = PATROL_FILE) -> None:
    path.write_text(json.dumps(asdict(patrol), indent=2), encoding="utf-8")
