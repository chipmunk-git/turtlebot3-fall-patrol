"""Visual annotations for fall-event snapshots, not the live video stream.

The existing patrol detects events. This module only draws overlays on a
matched, cached camera JPEG from that SAME observed person.
"""
import math
import cv2
import numpy as np

# COCO 17-keypoint pose skeleton (same edges as lying_detector_node.py).
SKELETON = ((5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12),
            (11, 12), (11, 13), (13, 15), (12, 14), (14, 16))
# OpenCV uses BGR. Cyan #00E5FF, red #FF3030 and orange #FF9500.
CYAN = (255, 229, 0)
RED = (48, 48, 255)
ORANGE = (0, 149, 255)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def annotate_event_photo(jpeg, annotation, result):
    """Return JPEG with one target's box and valid keypoints, or original.

    Never invent joints. Invalid metadata raises ValueError, and the caller
    deliberately falls back to the original camera photo.
    """
    if result not in ('confirmed', 'uncertain'):
        return jpeg
    if not isinstance(jpeg, bytes) or not isinstance(annotation, dict):
        raise ValueError('invalid JPEG/annotation')
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('cannot decode JPEG')
    h, w = img.shape[:2]
    source_size = annotation.get('image_size')
    if (not isinstance(source_size, (tuple, list)) or len(source_size) != 2 or
            not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                    for v in source_size)):
        return jpeg
    sw, sh = source_size
    sx, sy = w / sw, h / sh
    box = annotation.get('box')
    if (not isinstance(box, (list, tuple)) or len(box) != 4 or
            not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)):
        return jpeg
    x1, y1, x2, y2 = [int(round(v * mul)) for v, mul in zip(box, (sx, sy, sx, sy))]
    x1, x2 = sorted((min(w-1, max(0, x1)), min(w-1, max(0, x2))))
    y1, y2 = sorted((min(h-1, max(0, y1)), min(h-1, max(0, y2))))
    if x2-x1 < 3 or y2-y1 < 3:
        return jpeg
    color, name = (RED, 'Fall') if result == 'confirmed' else (ORANGE, 'Check')

    # Only sufficiently reliable joints already filtered by the detector.
    raw_points = annotation.get('keypoints')
    points = [None] * 17
    if isinstance(raw_points, list):
        for i, p in enumerate(raw_points[:17]):
            if p is None:
                continue
            if (not isinstance(p, (tuple, list)) or len(p) != 2 or
                    not all(isinstance(v, (int, float)) and math.isfinite(v) for v in p)):
                continue
            x, y = int(round(p[0] * sx)), int(round(p[1] * sy))
            if 0 <= x < w and 0 <= y < h:
                points[i] = (x, y)

    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
    for a, b in SKELETON:
        if points[a] is not None and points[b] is not None:
            cv2.line(img, points[a], points[b], CYAN, 2, cv2.LINE_AA)
    for point in points:
        if point is not None:
            cv2.circle(img, point, 4, CYAN, -1, cv2.LINE_AA)

    font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1
    (tw, th), _ = cv2.getTextSize(name, font, scale, thickness)
    label_w, label_h = tw + 12, th + 10
    lx = max(0, min(x1, w-label_w))
    ly = y1-label_h if y1 >= label_h else y1
    ly = max(0, min(ly, h-label_h))
    cv2.rectangle(img, (lx, ly), (lx+label_w, ly+label_h), color, -1)
    # Dark outline increases legibility on either alert color.
    cv2.putText(img, name, (lx+6, ly+label_h-6), font, scale, BLACK, thickness+2, cv2.LINE_AA)
    cv2.putText(img, name, (lx+6, ly+label_h-6), font, scale, WHITE, thickness, cv2.LINE_AA)
    ok, encoded = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError('cannot encode JPEG')
    return encoded.tobytes()
