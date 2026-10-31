import argparse
import math
from pathlib import Path

import yaml

from scripts.map_cleaner import clean_map, crop_to_content
from scripts.png_utils import decode_gray_png, encode_png

MAP_DIR = Path(__file__).resolve().parent.parent / "maps"


def shifted_origin(origin: list[float], dx: float, dy: float) -> list[float]:
    x, y, yaw = origin
    cos, sin = math.cos(yaw), math.sin(yaw)
    return [round(x + dx * cos - dy * sin, 4), round(y + dx * sin + dy * cos, 4), yaw]


def read_pgm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    tokens: list[bytes] = []
    position = 0
    while len(tokens) < 4:
        while data[position : position + 1].isspace():
            position += 1
        if data[position : position + 1] == b"#":
            position = data.index(b"\n", position)
            continue
        end = position
        while not data[end : end + 1].isspace():
            end += 1
        tokens.append(data[position:end])
        position = end
    magic, width, height, max_value = tokens[0], int(tokens[1]), int(tokens[2]), int(tokens[3])
    if magic == b"P5" and max_value < 256:
        return width, height, data[position + 1 : position + 1 + width * height]
    if magic == b"P2":
        values = data[position:].split()[: width * height]
        return width, height, bytes(int(value) * 255 // max_value for value in values)
    raise ValueError(f"unsupported pgm format: {magic.decode()} max={max_value}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ROS map.yaml + pgm을 대시보드용 maps/map.png, map.yaml로 변환")
    parser.add_argument("yaml_path", type=Path, help="SLAM으로 저장한 map.yaml 경로")
    parser.add_argument("--clean", action="store_true", help="방 밖 노이즈 제거, 벽 틈 메우기, 벽 두께 정리")
    parser.add_argument("--pretty", action="store_true", help="--clean 후 벽을 직선으로 펴고 고해상도로 다시 그림")
    args = parser.parse_args()

    meta = yaml.safe_load(args.yaml_path.read_text(encoding="utf-8"))
    image_path = args.yaml_path.parent / meta["image"]
    if image_path.suffix.lower() == ".png":
        width, height, pixels = decode_gray_png(image_path.read_bytes())
    else:
        width, height, pixels = read_pgm(image_path)
    if args.clean or args.pretty:
        pixels = clean_map(width, height, pixels)
        width, height, pixels, left, bottom = crop_to_content(width, height, pixels)
        meta["origin"] = shifted_origin(
            meta["origin"], left * meta["resolution"], bottom * meta["resolution"]
        )
    if args.pretty:
        from scripts.map_vectorizer import prettify_map

        new_width, new_height, pixels, corner, yaw = prettify_map(width, height, pixels)
        resolution = meta["resolution"]
        meta["origin"] = shifted_origin(
            meta["origin"], corner[0] * resolution, (height - corner[1]) * resolution
        )
        meta["origin"][2] = round(meta["origin"][2] + yaw, 6)
        meta["resolution"] = resolution / 8
        width, height = new_width, new_height

    MAP_DIR.mkdir(exist_ok=True)
    (MAP_DIR / "map.png").write_bytes(encode_png(width, height, pixels, 1))
    meta["image"] = "map.png"
    (MAP_DIR / "map.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    print(f"converted {width}x{height} map → {MAP_DIR}")


if __name__ == "__main__":
    main()
