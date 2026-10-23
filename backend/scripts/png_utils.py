import random
import struct
import zlib

COLOR_TYPES = {1: 0, 3: 2}


def encode_png(width: int, height: int, pixels: bytes, channels: int) -> bytes:
    stride = width * channels
    raw = b"".join(b"\x00" + pixels[row * stride : (row + 1) * stride] for row in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, COLOR_TYPES[channels], 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def decode_gray_png(data: bytes) -> tuple[int, int, bytes]:
    position = 8
    width = height = 0
    compressed = b""
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        tag = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", body[:10])
            if depth != 8 or color_type != 0:
                raise ValueError("only 8-bit grayscale png is supported")
        elif tag == b"IDAT":
            compressed += body
        position += 12 + length
    raw = zlib.decompress(compressed)
    stride = width + 1
    if any(raw[row * stride] != 0 for row in range(height)):
        raise ValueError("only unfiltered png rows are supported")
    return width, height, b"".join(raw[row * stride + 1 : (row + 1) * stride] for row in range(height))


SNAPSHOT_SCALES = {"detected": 0.45, "observing": 1.0, "decision": 1.0}
BOX_COLORS = {"detected": (0, 230, 118), "observing": (0, 230, 118), "decision": (255, 82, 110)}


def make_snapshot(seed: int, kind: str = "observing", width: int = 320, height: int = 240) -> bytes:
    rng = random.Random(seed)
    floor = (rng.randint(150, 200), rng.randint(130, 170), rng.randint(100, 140))
    shirt = (rng.randint(30, 220), rng.randint(30, 220), rng.randint(30, 220))
    skin = (230, 190, 160)
    pants = (60, 60, 90)
    pixels = bytearray(floor * (width * height))

    def fill_rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(max(y0, 0), min(y1, height)):
            for x in range(max(x0, 0), min(x1, width)):
                i = (y * width + x) * 3
                pixels[i : i + 3] = bytes(color)

    def fill_circle(cx: int, cy: int, r: int, color: tuple[int, int, int]) -> None:
        for y in range(cy - r, cy + r):
            for x in range(cx - r, cx + r):
                if 0 <= x < width and 0 <= y < height and (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    i = (y * width + x) * 3
                    pixels[i : i + 3] = bytes(color)

    scale = SNAPSHOT_SCALES[kind]
    horizon = height // 3 if scale == 1.0 else height // 2
    fill_rect(0, 0, width, horizon, (225, 225, 215))

    body, legs, head = round(120 * scale), round(80 * scale), round(16 * scale)
    thickness = round(30 * scale)
    if scale == 1.0:
        left, top = 40 + rng.randint(0, 50), 130 + rng.randint(0, 40)
    else:
        left, top = 90 + rng.randint(0, 60), horizon + 25 + rng.randint(0, 20)
    if kind == "decision":
        left += 1
    fill_rect(left, top, left + body, top + thickness, shirt)
    fill_rect(left + body, top + thickness // 7, left + body + legs, top + thickness - thickness // 7, pants)
    fill_circle(left - head + 2, top + thickness // 2, head, skin)

    color = BOX_COLORS[kind]
    pad = 6
    x0, y0 = left - 2 * head - pad, top + thickness // 2 - head - pad
    x1, y1 = left + body + legs + pad, top + thickness // 2 + head + pad
    for offset in range(2):
        fill_rect(x0 + offset, y0, x0 + offset + 1, y1, color)
        fill_rect(x1 - offset - 1, y0, x1 - offset, y1, color)
        fill_rect(x0, y0 + offset, x1, y0 + offset + 1, color)
        fill_rect(x0, y1 - offset - 1, x1, y1 - offset, color)
    fill_rect(x0, y0 - 10, x0 + 46, y0, color)
    return encode_png(width, height, bytes(pixels), 3)
