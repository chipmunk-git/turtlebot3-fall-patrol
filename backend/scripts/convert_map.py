import argparse
from pathlib import Path

import yaml

from scripts.map_cleaner import clean_map, crop_to_content
from scripts.png_utils import encode_png

MAP_DIR = Path(__file__).resolve().parent.parent / "maps"


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
    args = parser.parse_args()

    meta = yaml.safe_load(args.yaml_path.read_text(encoding="utf-8"))
    width, height, pixels = read_pgm(args.yaml_path.parent / meta["image"])
    if args.clean:
        pixels = clean_map(width, height, pixels)
        width, height, pixels, left, bottom = crop_to_content(width, height, pixels)
        meta["origin"] = [
            round(meta["origin"][0] + left * meta["resolution"], 4),
            round(meta["origin"][1] + bottom * meta["resolution"], 4),
            meta["origin"][2],
        ]

    MAP_DIR.mkdir(exist_ok=True)
    (MAP_DIR / "map.png").write_bytes(encode_png(width, height, pixels, 1))
    meta["image"] = "map.png"
    (MAP_DIR / "map.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    print(f"converted {width}x{height} map → {MAP_DIR}")


if __name__ == "__main__":
    main()
