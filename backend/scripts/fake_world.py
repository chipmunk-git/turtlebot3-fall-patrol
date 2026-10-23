from pathlib import Path

from scripts.patrol import Patrol, save_patrol
from scripts.png_utils import encode_png

RESOLUTION = 0.05
ORIGIN = (-10.0, -6.0)
WIDTH_M = 20.0
HEIGHT_M = 12.0

FREE = 254
OCCUPIED = 0
UNKNOWN = 205

WAYPOINTS = [(-7.0, -3.0), (7.0, -3.0), (7.0, 3.0), (-7.0, 3.0)]
PERSON_SPOTS = [(-4.0, -4.2), (8.0, 1.0), (3.0, 4.2)]

Rect = tuple[float, float, float, float]

ROOM: Rect = (-9.0, -5.0, 9.0, 5.0)
WALLS: list[Rect] = [
    (-9.0, -5.0, 9.0, -4.9),
    (-9.0, 4.9, 9.0, 5.0),
    (-9.0, -5.0, -8.9, 5.0),
    (8.9, -5.0, 9.0, 5.0),
    (-0.05, -5.0, 0.05, -3.8),
    (-0.05, -2.2, 0.05, 2.2),
    (-0.05, 3.8, 0.05, 5.0),
]
FURNITURE: list[Rect] = [(-4.0, -1.0, -2.0, 1.0), (2.0, -1.0, 4.0, 1.0)]


def render_map() -> tuple[int, int, bytes]:
    width = round(WIDTH_M / RESOLUTION)
    height = round(HEIGHT_M / RESOLUTION)
    pixels = bytearray([UNKNOWN]) * (width * height)

    def fill(rect: Rect, value: int) -> None:
        x0, y0, x1, y1 = rect
        col0 = int((x0 - ORIGIN[0]) / RESOLUTION)
        col1 = int((x1 - ORIGIN[0]) / RESOLUTION)
        row0 = height - int((y1 - ORIGIN[1]) / RESOLUTION)
        row1 = height - int((y0 - ORIGIN[1]) / RESOLUTION)
        for row in range(max(row0, 0), min(row1, height)):
            start = row * width
            pixels[start + max(col0, 0) : start + min(col1, width)] = bytes([value]) * (
                min(col1, width) - max(col0, 0)
            )

    fill(ROOM, FREE)
    for rect in WALLS + FURNITURE:
        fill(rect, OCCUPIED)
    return width, height, bytes(pixels)


def write_map(map_dir: Path) -> None:
    map_dir.mkdir(parents=True, exist_ok=True)
    width, height, pixels = render_map()
    (map_dir / "map.png").write_bytes(encode_png(width, height, pixels, 1))
    (map_dir / "map.yaml").write_text(
        "image: map.png\n"
        "mode: trinary\n"
        f"resolution: {RESOLUTION}\n"
        f"origin: [{ORIGIN[0]}, {ORIGIN[1]}, 0.0]\n"
        "negate: 0\n"
        "occupied_thresh: 0.65\n"
        "free_thresh: 0.25\n",
        encoding="utf-8",
    )
    save_patrol(Patrol(WAYPOINTS, PERSON_SPOTS, detect_range=2.5, approach_distance=0.8), map_dir / "patrol.json")


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "maps"
    write_map(target)
    print(f"fake map written to {target}")
