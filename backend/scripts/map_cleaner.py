from collections import deque

FREE = 254
OCCUPIED = 0
UNKNOWN = 205

Mask = list[bool]


class Grid:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def neighbors(self, index: int, diagonal: bool = True) -> list[int]:
        x, y = index % self.width, index // self.width
        steps = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        if diagonal:
            steps += [(-1, -1), (1, -1), (-1, 1), (1, 1)]
        return [
            (y + dy) * self.width + (x + dx)
            for dx, dy in steps
            if 0 <= x + dx < self.width and 0 <= y + dy < self.height
        ]

    def dilate(self, mask: Mask, radius: int) -> Mask:
        result = mask[:]
        for _ in range(radius):
            result = [value or any(result[n] for n in self.neighbors(i)) for i, value in enumerate(result)]
        return result

    def erode(self, mask: Mask, radius: int) -> Mask:
        result = mask[:]
        for _ in range(radius):
            result = [value and all(result[n] for n in self.neighbors(i)) for i, value in enumerate(result)]
        return result

    def components(self, mask: Mask) -> list[list[int]]:
        seen = [False] * len(mask)
        found = []
        for start, value in enumerate(mask):
            if not value or seen[start]:
                continue
            seen[start] = True
            queue = deque([start])
            component = []
            while queue:
                index = queue.popleft()
                component.append(index)
                for n in self.neighbors(index, diagonal=False):
                    if mask[n] and not seen[n]:
                        seen[n] = True
                        queue.append(n)
            found.append(component)
        return found


def crop_to_content(
    width: int, height: int, pixels: bytes, margin: int = 4
) -> tuple[int, int, bytes, int, int]:
    rows = [y for y in range(height) if any(pixels[y * width + x] != UNKNOWN for x in range(width))]
    cols = [x for x in range(width) if any(pixels[y * width + x] != UNKNOWN for y in range(height))]
    left, right = max(cols[0] - margin, 0), min(cols[-1] + margin, width - 1)
    top, bottom = max(rows[0] - margin, 0), min(rows[-1] + margin, height - 1)
    cropped = b"".join(pixels[y * width + left : y * width + right + 1] for y in range(top, bottom + 1))
    return right - left + 1, bottom - top + 1, cropped, left, height - 1 - bottom


def clean_map(width: int, height: int, pixels: bytes, wall_thickness: int = 2) -> bytes:
    grid = Grid(width, height)
    occupied = grid.erode(grid.dilate([value < 100 for value in pixels], 1), 1)
    free = [value > 250 and not wall for value, wall in zip(pixels, occupied)]

    room = max(grid.components(free), key=len)
    free = [False] * len(free)
    for index in room:
        free[index] = True

    for _ in range(2):
        counts = [sum(free[n] for n in grid.neighbors(i)) for i in range(len(free))]
        free = [
            (is_free and counts[i] > 2) or (not is_free and not occupied[i] and counts[i] >= 5)
            for i, is_free in enumerate(free)
        ]

    for hole in grid.components([not value for value in free]):
        touches_edge = any(
            i % width in (0, width - 1) or i // width in (0, height - 1) for i in hole
        )
        if not touches_edge and len(hole) < 10:
            for index in hole:
                free[index] = True

    free = [
        is_free or sum(free[n] for n in grid.neighbors(i, diagonal=False)) >= 3
        for i, is_free in enumerate(free)
    ]

    walls = grid.dilate(free, wall_thickness)
    return bytes(
        FREE if is_free else OCCUPIED if is_wall else UNKNOWN for is_free, is_wall in zip(free, walls)
    )
