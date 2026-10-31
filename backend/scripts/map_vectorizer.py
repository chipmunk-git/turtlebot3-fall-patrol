import math

from PIL import Image, ImageDraw, ImageFilter

from scripts.map_cleaner import FREE, OCCUPIED, UNKNOWN

Point = tuple[float, float]
Loop = list[Point]

RIGHT_TURN = {(1, 0): (0, 1), (0, 1): (-1, 0), (-1, 0): (0, -1), (0, -1): (1, 0)}


def boundary_loops(width: int, height: int, free: list[bool]) -> list[Loop]:
    def is_free(x: int, y: int) -> bool:
        return 0 <= x < width and 0 <= y < height and free[y * width + x]

    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(start: tuple[int, int], end: tuple[int, int]) -> None:
        edges.setdefault(start, []).append(end)

    for y in range(height):
        for x in range(width):
            if not free[y * width + x]:
                continue
            if not is_free(x, y - 1):
                add((x, y), (x + 1, y))
            if not is_free(x + 1, y):
                add((x + 1, y), (x + 1, y + 1))
            if not is_free(x, y + 1):
                add((x + 1, y + 1), (x, y + 1))
            if not is_free(x - 1, y):
                add((x, y + 1), (x, y))

    loops: list[Loop] = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        current = start
        direction: tuple[int, int] | None = None
        while True:
            options = edges[current]
            choice = options[0]
            if direction is not None and len(options) > 1:
                preferred = RIGHT_TURN[direction]
                for option in options:
                    if (option[0] - current[0], option[1] - current[1]) == preferred:
                        choice = option
            options.remove(choice)
            if not options:
                del edges[current]
            direction = (choice[0] - current[0], choice[1] - current[1])
            current = choice
            if current == start:
                break
            loop.append(current)
        loops.append([(float(x), float(y)) for x, y in drop_collinear(loop)])
    return loops


def drop_collinear(loop: list[tuple[int, int]]) -> list[tuple[int, int]]:
    kept = []
    count = len(loop)
    for i, point in enumerate(loop):
        before, after = loop[i - 1], loop[(i + 1) % count]
        cross = (point[0] - before[0]) * (after[1] - point[1]) - (point[1] - before[1]) * (after[0] - point[0])
        if cross != 0:
            kept.append(point)
    return kept


def signed_area(loop: Loop) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(loop, loop[1:] + loop[:1])) / 2


def point_line_distance(point: Point, start: Point, end: Point) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return math.dist(point, start)
    return abs(dx * (start[1] - point[1]) - dy * (start[0] - point[0])) / length


def douglas_peucker(points: list[Point], epsilon: float) -> list[Point]:
    if len(points) < 3:
        return points
    index, distance = max(
        ((i, point_line_distance(points[i], points[0], points[-1])) for i in range(1, len(points) - 1)),
        key=lambda item: item[1],
    )
    if distance <= epsilon:
        return [points[0], points[-1]]
    return douglas_peucker(points[: index + 1], epsilon)[:-1] + douglas_peucker(points[index:], epsilon)


def simplify_loop(loop: Loop, epsilon: float) -> Loop:
    if len(loop) < 4:
        return loop
    far = max(range(len(loop)), key=lambda i: math.dist(loop[0], loop[i]))
    first = douglas_peucker(loop[: far + 1], epsilon)
    second = douglas_peucker(loop[far:] + [loop[0]], epsilon)
    return first[:-1] + second[:-1]


def centroid(loop: Loop) -> Point:
    area = signed_area(loop)
    cx = cy = 0.0
    for a, b in zip(loop, loop[1:] + loop[:1]):
        cross = a[0] * b[1] - b[0] * a[1]
        cx += (a[0] + b[0]) * cross
        cy += (a[1] + b[1]) * cross
    return cx / (6 * area), cy / (6 * area)


def dominant_angle(loops: list[Loop]) -> float:
    c = s = 0.0
    for loop in loops:
        for a, b in zip(loop, loop[1:] + loop[:1]):
            length = math.dist(a, b)
            angle = math.atan2(b[1] - a[1], b[0] - a[0])
            c += length * math.cos(4 * angle)
            s += length * math.sin(4 * angle)
    return math.atan2(s, c) / 4


def snap_direction(a: Point, b: Point, base: float, tolerance: float) -> float:
    angle = math.atan2(b[1] - a[1], b[0] - a[0])
    nearest = base + round((angle - base) / (math.pi / 2)) * (math.pi / 2)
    return nearest if abs(angle - nearest) <= tolerance else angle


def straighten_loop(
    loop: Loop,
    base: float,
    tolerance: float,
    merge_distance: float,
    max_shift: float = 3.0,
    short_edge: float = 8.0,
) -> Loop:
    lines = []
    for a, b in zip(loop, loop[1:] + loop[:1]):
        angle = snap_direction(a, b, base, tolerance)
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        lines.append([mid, angle, math.dist(a, b), a, max_shift])

    merged = True
    while merged and len(lines) > 3:
        merged = False
        for i in range(len(lines)):
            first, second = lines[i], lines[(i + 1) % len(lines)]
            if abs(math.sin(first[1] - second[1])) > 1e-6:
                continue
            offset = abs(
                (second[0][0] - first[0][0]) * math.sin(first[1]) - (second[0][1] - first[0][1]) * math.cos(first[1])
            )
            if offset > merge_distance:
                continue
            total = first[2] + second[2]
            first[0] = (
                (first[0][0] * first[2] + second[0][0] * second[2]) / total,
                (first[0][1] * first[2] + second[0][1] * second[2]) / total,
            )
            first[2] = total
            lines.pop((i + 1) % len(lines))
            merged = True
            break

    def snapped(angle: float) -> bool:
        return abs(math.sin(2 * (angle - base))) < 1e-6

    changed = True
    while changed and len(lines) > 4:
        changed = False
        for i, line in enumerate(lines):
            prev_line, next_line = lines[i - 1], lines[(i + 1) % len(lines)]
            if snapped(line[1]) or line[2] > short_edge or not (snapped(prev_line[1]) and snapped(next_line[1])):
                continue
            turn = abs(math.sin(prev_line[1] - next_line[1]))
            if turn > 0.9:
                next_line[3], next_line[4] = line[0], short_edge
                lines.pop(i)
            elif turn < 0.1:
                line[1] = prev_line[1] + math.pi / 2
                line[4] = next_line[4] = short_edge
            else:
                continue
            changed = True
            break

    vertices: Loop = []
    for i, (point, angle, _, corner, shift) in enumerate(lines):
        prev_point, prev_angle, _, _, _ = lines[i - 1]
        crossing = intersect(prev_point, prev_angle, point, angle)
        if crossing is not None and math.dist(crossing, corner) <= shift:
            vertices.append(crossing)
        else:
            vertices.append(project(prev_point, prev_angle, corner))
            vertices.append(project(point, angle, corner))
    return vertices


def intersect(p1: Point, a1: float, p2: Point, a2: float) -> Point | None:
    d1, d2 = (math.cos(a1), math.sin(a1)), (math.cos(a2), math.sin(a2))
    denominator = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(denominator) < 0.2:
        return None
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / denominator
    return p1[0] + t * d1[0], p1[1] + t * d1[1]


def project(point: Point, angle: float, target: Point) -> Point:
    dx, dy = math.cos(angle), math.sin(angle)
    t = (target[0] - point[0]) * dx + (target[1] - point[1]) * dy
    return point[0] + t * dx, point[1] + t * dy


def prettify_map(
    width: int,
    height: int,
    pixels: bytes,
    scale: int = 8,
    epsilon: float = 1.8,
    small_epsilon: float = 0.8,
    small_perimeter: float = 60.0,
    tolerance_deg: float = 12.0,
    wall_width: float = 1.6,
) -> tuple[int, int, bytes, Point, float]:
    free = [value == FREE for value in pixels]
    loops = [loop for loop in boundary_loops(width, height, free) if abs(signed_area(loop)) >= 2]

    def perimeter(loop: Loop) -> float:
        return sum(math.dist(a, b) for a, b in zip(loop, loop[1:] + loop[:1]))

    outer_sign = math.copysign(1, signed_area(max(loops, key=lambda loop: abs(signed_area(loop)))))
    is_hole = [math.copysign(1, signed_area(loop)) != outer_sign for loop in loops]
    is_small = [perimeter(loop) <= small_perimeter for loop in loops]

    simplified = [simplify_loop(loop, small_epsilon if small else epsilon) for loop, small in zip(loops, is_small)]
    base = dominant_angle([loop for loop, small in zip(simplified, is_small) if not small] or simplified)
    tolerance = math.radians(tolerance_deg)
    straight = [straighten_loop(loop, base, tolerance, epsilon) for loop in simplified]

    # 벽이 화면과 나란하도록 지도 중심을 기준으로 -base만큼 돌려서 그린다.
    cos, sin = math.cos(-base), math.sin(-base)
    center = (width / 2, height / 2)

    def rotate(point: Point) -> Point:
        dx, dy = point[0] - center[0], point[1] - center[1]
        return dx * cos - dy * sin, dx * sin + dy * cos

    rotated = [[rotate(point) for point in loop] for loop in straight]
    xs = [x for loop in rotated for x, _ in loop]
    ys = [y for loop in rotated for _, y in loop]
    margin = 2 * wall_width
    left, top = min(xs) - margin, min(ys) - margin
    width, height = math.ceil(max(xs) + margin - left), math.ceil(max(ys) + margin - top)

    def place(point: Point) -> Point:
        x, y = rotate(point)
        return x - left, y - top

    straight = [[(x - left, y - top) for x, y in loop] for loop in rotated]
    centers = [place(centroid(loop)) for loop in loops]

    size = (width * scale, height * scale)
    free_mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(free_mask)
    order = sorted(range(len(loops)), key=lambda i: -abs(signed_area(loops[i])))
    for i in order:
        if is_hole[i] and is_small[i]:
            cx, cy = centers[i]
            radius = math.sqrt(abs(signed_area(loops[i])) / math.pi)
            draw.ellipse(
                [(cx - radius) * scale, (cy - radius) * scale, (cx + radius) * scale, (cy + radius) * scale], fill=0
            )
        else:
            draw.polygon([(x * scale, y * scale) for x, y in straight[i]], fill=0 if is_hole[i] else 255)

    wall_mask = free_mask.filter(ImageFilter.MaxFilter(2 * round(wall_width * scale) + 1))
    image = Image.new("L", size, UNKNOWN)
    image.paste(OCCUPIED, mask=wall_mask)
    image.paste(FREE, mask=free_mask)
    pillars = ImageDraw.Draw(image)
    for i in order:
        if is_hole[i] and is_small[i]:
            cx, cy = centers[i]
            radius = math.sqrt(abs(signed_area(loops[i])) / math.pi)
            pillars.ellipse(
                [(cx - radius) * scale, (cy - radius) * scale, (cx + radius) * scale, (cy + radius) * scale],
                fill=OCCUPIED,
            )

    # 새 그림의 왼쪽 아래 모서리가 원래 그림에서 어디인지 (ROS map.yaml origin 계산용)
    dx, dy = left, top + height
    corner = (center[0] + dx * cos + dy * sin, center[1] - dx * sin + dy * cos)
    return image.width, image.height, image.tobytes(), corner, -base
