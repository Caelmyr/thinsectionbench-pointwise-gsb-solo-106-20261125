"""Geometry for field views, raster masks, profiles and annotation measurement."""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

from .domain import DEFECTS, PALETTE, Field, InputError, Vec, field, point, polygon, segment

PIXEL_BUDGET = 12_000_000
GUIDE_LIMIT = 9_000
EPSILON = 1e-9


def roi_slices(box, dimensions):
    w, h = dimensions
    corners = box.corners()
    x0 = max(0, math.floor(min(p.x for p in corners)))
    y0 = max(0, math.floor(min(p.y for p in corners)))
    x1 = min(w, math.ceil(max(p.x for p in corners)))
    y1 = min(h, math.ceil(max(p.y for p in corners)))
    return slice(y0, max(y0, y1)), slice(x0, max(x0, x1))


def local_array_coordinates(box, rows, cols):
    u, v = box.axes
    xx = np.arange(cols.start, cols.stop, dtype=np.float32)[None, :] + (0.5 - box.origin.x)
    yy = np.arange(rows.start, rows.stop, dtype=np.float32)[:, None] + (0.5 - box.origin.y)
    return xx * u.x + yy * u.y, xx * v.x + yy * v.y


def rectangle_slice_mask(box, dimensions):
    rows, cols = roi_slices(box, dimensions)
    local_u, local_v = local_array_coordinates(box, rows, cols)
    mask = (local_u >= 0) & (local_u < box.width) & (local_v >= 0) & (local_v < box.height)
    return rows, cols, mask


def rectangle_mask(box, dimensions):
    w, h = dimensions
    mask = np.zeros((h, w), dtype=bool)
    rows, cols, included = rectangle_slice_mask(box, dimensions)
    mask[rows, cols] = included
    return mask


def segmentation_masks(state, dimensions):
    w, h = dimensions
    viewport = field(state["field"])
    selected = rectangle_mask(viewport, dimensions)
    discarded = np.zeros((h, w), dtype=bool)
    for item in state["defects"]:
        rows, cols, mask = rectangle_slice_mask(field(item["shape"]), dimensions)
        discarded[rows, cols] |= mask
    return selected, selected & ~discarded


def field_summary(state, dimensions):
    selected, kept = segmentation_masks(state, dimensions)
    total = int(np.count_nonzero(selected))
    usable = int(np.count_nonzero(kept))
    summary = []
    visible_by_type = Counter()
    for item in state["defects"]:
        rows, cols, mask = rectangle_slice_mask(field(item["shape"]), dimensions)
        visible = int(np.count_nonzero(mask & selected[rows, cols]))
        summary.append({
            "id": item["id"],
            "kind": item["kind"],
            "label": DEFECTS[item["kind"]],
            "visible_pixels": visible,
        })
        visible_by_type[item["kind"]] += visible
    return {
        "field_pixels": total,
        "usable_pixels": usable,
        "excluded_pixels": total - usable,
        "usable_percent": 100 * usable / total if total else None,
        "defects": summary,
        "marked_pixels_by_kind": dict(visible_by_type),
    }


def stage_guide(field_spec, grid_spec, dimensions):
    """Centers of a single operator-aligned guide, clipped to the slide image."""
    box = field(field_spec)
    step = grid_spec["step"]
    u_first = step * (0.25 if grid_spec["phase_u"] == "early" else 0.75)
    v_first = step * (0.25 if grid_spec["phase_v"] == "early" else 0.75)
    ncol = max(0, math.ceil((box.width - u_first) / step))
    nrow = max(0, math.ceil((box.height - v_first) / step))
    if ncol * nrow > GUIDE_LIMIT:
        return {"positions": [], "message": "引导点太密，请增大台阶间距", "count": None}
    w, h = dimensions
    positions = []
    for row in range(nrow):
        for column in range(ncol):
            u = u_first + column * step
            v = v_first + row * step
            center = box.to_source(Vec(u, v))
            if 0 <= center.x < w and 0 <= center.y < h:
                positions.append({
                    "column": column,
                    "row": row,
                    "u": u,
                    "v": v,
                    **center.json(),
                })
    return {
        "positions": positions,
        "message": "",
        "count": len(positions),
        "offset_u": u_first,
        "offset_v": v_first,
    }


def manual_tally(records):
    counts = Counter(item["mineral"] for item in records)
    total = len(records)
    known = counts.total() - counts["undetermined"]
    minerals = []
    for key, entry in PALETTE.items():
        minerals.append({
            "key": key,
            **entry,
            "count": counts[key],
            "percent": 100 * counts[key] / total if total else None,
        })
    return {
        "count": total,
        "classified": known,
        "undetermined": total - known,
        "minerals": minerals,
    }


def line_coordinates(line, spacing):
    n = max(2, int(math.ceil(line.length / spacing)) + 1)
    if n > 16_384:
        raise InputError("测线采样过密")
    t = np.linspace(0, 1, n)
    x = line.start.x + (line.end.x - line.start.x) * t
    y = line.start.y + (line.end.y - line.start.y) * t
    return t, x, y


def physical_line_samples(line, length_um, spacing_um):
    """Samples at a fixed physical µm pitch, always keeping the end point."""
    steps = int(math.floor(length_um / spacing_um - 1e-9))
    fractions = [index * spacing_um / length_um for index in range(steps + 1)]
    if not fractions or fractions[-1] < 1 - 1e-9:
        fractions.append(1.0)
    t = np.asarray(fractions, dtype=np.float64)
    if t.size > 16_384:
        raise InputError("测线采样过密")
    x = line.start.x + (line.end.x - line.start.x) * t
    y = line.start.y + (line.end.y - line.start.y) * t
    return t, x, y


def bilinear_values(image, x, y):
    # The first pixel center lies at (0.5, 0.5) in source coordinates.
    h, w = image.shape[:2]
    x = np.clip(x - 0.5, 0, w - 1)
    y = np.clip(y - 0.5, 0, h - 1)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(w - 1, x0 + 1)
    y1 = np.minimum(h - 1, y0 + 1)
    dx = x - x0
    dy = y - y0
    if image.ndim == 3:
        dx = np.expand_dims(dx, axis=-1)
        dy = np.expand_dims(dy, axis=-1)
    top = image[y0, x0] * (1 - dx) + image[y0, x1] * dx
    bottom = image[y1, x0] * (1 - dx) + image[y1, x1] * dx
    return top * (1 - dy) + bottom * dy


def signed_polygon_area(vertices):
    return sum(a.cross(b) for a, b in zip(vertices, vertices[1:] + vertices[:1])) / 2


def polygon_centroid(vertices):
    area = signed_polygon_area(vertices)
    if abs(area) <= EPSILON:
        raise InputError("轮廓无法形成有效中心")
    x = y = 0.0
    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
        cross = a.cross(b)
        x += (a.x + b.x) * cross
        y += (a.y + b.y) * cross
    return Vec(x / (6 * area), y / (6 * area))


def _orientation(a, b, c):
    return (b - a).cross(c - a)


def _point_in_box(q, a, b):
    return (
        min(a.x, b.x) - EPSILON <= q.x <= max(a.x, b.x) + EPSILON
        and min(a.y, b.y) - EPSILON <= q.y <= max(a.y, b.y) + EPSILON
    )


def intersects(a, b, c, d):
    ac = _orientation(a, b, c)
    ad = _orientation(a, b, d)
    ca = _orientation(c, d, a)
    cb = _orientation(c, d, b)
    if ((ac < 0 < ad or ad < 0 < ac) and (ca < 0 < cb or cb < 0 < ca)):
        return True
    collinear = (
        (ac, c, a, b),
        (ad, d, a, b),
        (ca, a, c, d),
        (cb, b, c, d),
    )
    return any(abs(o) <= EPSILON and _point_in_box(q, s, e) for o, q, s, e in collinear)


def check_simple_polygon(vertices):
    n = len(vertices)
    for i in range(n):
        a, b = vertices[i], vertices[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            c, d = vertices[j], vertices[(j + 1) % n]
            if intersects(a, b, c, d):
                raise InputError(f"轮廓边 {i + 1} 与边 {j + 1} 交叉")


def convex_hull(vertices):
    ordered = sorted(set(vertices), key=lambda value: (value.x, value.y))
    if len(ordered) < 3:
        return ordered

    def arc(sequence):
        result = []
        for position in sequence:
            while len(result) > 1 and _orientation(result[-2], result[-1], position) <= EPSILON:
                result.pop()
            result.append(position)
        return result

    return arc(ordered)[:-1] + arc(reversed(ordered))[:-1]


def caliper_geometry(vertices):
    hull = convex_hull(vertices)
    if len(hull) < 3:
        raise InputError("轮廓凸包退化")
    diameter = 0.0
    axis = 0.0
    for i, a in enumerate(hull):
        for b in hull[i + 1:]:
            delta = b - a
            length = delta.norm()
            if length > diameter:
                diameter = length
                axis = math.degrees(math.atan2(delta.y, delta.x)) % 180
    minimum = math.inf
    enclosing = None
    enclosing_area = math.inf
    for a, b in zip(hull, hull[1:] + hull[:1]):
        vector = b - a
        length = vector.norm()
        u = vector * (1 / length)
        v = Vec(-u.y, u.x)
        us = [p.dot(u) for p in hull]
        vs = [p.dot(v) for p in hull]
        width = max(us) - min(us)
        height = max(vs) - min(vs)
        minimum = min(height, minimum)
        area = width * height
        if area < enclosing_area:
            origin = u * min(us) + v * min(vs)
            angle = math.degrees(math.atan2(u.y, u.x))
            enclosing = Field(origin, width, height, angle).json()
            enclosing_area = area
    return diameter, minimum, axis, enclosing


def grain_metrics(vertices, microns=None, area_scale=None):
    outline = polygon(vertices)
    check_simple_polygon(outline)
    area = abs(signed_polygon_area(outline))
    perimeter = sum((b - a).norm() for a, b in zip(outline, outline[1:] + outline[:1]))
    diameter, width, bearing, bounding = caliper_geometry(outline)
    metrics = {
        "centroid": polygon_centroid(outline).json(),
        "area_px2": area,
        "perimeter_px": perimeter,
        "feret_max_px": diameter,
        "feret_min_px": width,
        "equivalent_diameter_px": 2 * math.sqrt(area / math.pi),
        "long_axis_deg": bearing,
        "aspect_ratio": diameter / width,
        "circularity": min(1.0, 4 * math.pi * area / (perimeter ** 2)),
        "convexity": area / abs(signed_polygon_area(convex_hull(outline))),
        "enclosing": bounding,
    }
    if microns is not None:
        area_scale = microns ** 2 if area_scale is None else area_scale
        metrics["area_um2"] = area * area_scale
        for key in ("perimeter", "feret_max", "feret_min", "equivalent_diameter"):
            metrics[f"{key}_um"] = metrics[f"{key}_px"] * microns
    return metrics


def rose_for_grains(grains, bins):
    edges = np.linspace(0, 180, bins + 1)
    records = [(record["metrics"]["long_axis_deg"], record["metrics"]["area_px2"]) for record in grains]
    if not records:
        return {
            "bins": [{"from": float(a), "to": float(b), "count": 0, "area": 0.0}
                     for a, b in zip(edges, edges[1:])],
            "preferred_deg": None,
            "strength": None,
            "count": 0,
        }
    angle, weights = np.array(records).T
    counts = np.histogram(angle, bins=edges)[0]
    weighted = np.histogram(angle, bins=edges, weights=weights)[0]
    angles = np.deg2rad(angle) * 2
    resultant = np.sum(weights * np.exp(1j * angles)) / np.sum(weights)
    return {
        "bins": [
            {"from": float(a), "to": float(b), "count": int(n), "area": float(area)}
            for a, b, n, area in zip(edges, edges[1:], counts, weighted)
        ],
        "preferred_deg": math.degrees(np.angle(resultant) / 2) % 180 if abs(resultant) > 1e-8 else None,
        "strength": float(abs(resultant)),
        "count": len(grains),
    }


def sampled_pixel(image, coordinates):
    value = point(coordinates, dimensions=(image.shape[1], image.shape[0]))
    x, y = int(value.x), int(value.y)
    rgb = image[y, x]
    red, green, blue = (int(c) for c in rgb[:3])
    return {
        "pixel_x": x,
        "pixel_y": y,
        "r": red,
        "g": green,
        "b": blue,
        "luminance": round(0.2126 * red + 0.7152 * green + 0.0722 * blue, 3),
    }


def physical_segment(specification, calibration):
    line = segment(specification)
    if calibration is None:
        length_um = None
    elif calibration.get("mode") == "anisotropic":
        delta = line.end - line.start
        length_um = math.hypot(
            delta.x * calibration["um_per_px_x"],
            delta.y * calibration["um_per_px_y"],
        )
    else:
        length_um = line.length * calibration["um_per_px"]
    return {
        "length_px": line.length,
        "length_um": length_um,
        "bearing_deg": line.bearing,
    }
