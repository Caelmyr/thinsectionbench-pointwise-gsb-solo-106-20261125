"""Recommended, pixel-centre cross-sections inside hand-traced grains.

A retained section is one four-connected component of pixels whose centres are
simultaneously inside the source image, hand polygon and observation field and
outside every marked waste region.  Measurements are for whole pixel cells, so
a run of N pixels has an outer-edge length of N rather than the centre span
N - 1.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy import ndimage

from .domain import InputError, Vec, field, polygon as clean_polygon
from .geometry import (
    caliper_geometry, convex_hull, grain_metrics, rectangle_slice_mask,
)

SECTION_VERSION = 1
FOUR_NEIGHBOUR = np.array(((0, 1, 0), (1, 1, 1), (0, 1, 0)), dtype=bool)
# Imported packages are part of requirements; keep the optional acceleration
# reference local-ish to make the pure geometry easy to test without image IO.
try:  # pragma: no cover - import guard depends on the runtime environment
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def polygon_center_mask(vertices, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """Ray-cast pixel centres in the inclusive ROI; boundary centres count."""
    cols = np.arange(x0, x1, dtype=np.float64)[None, :] + 0.5
    rows = np.arange(y0, y1, dtype=np.float64)[:, None] + 0.5
    inside = np.zeros((y1 - y0, x1 - x0), dtype=bool)

    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    for a, b in edges:
        rise = b.y - a.y
        if abs(rise) >= 1e-12:
            crosses = ((a.y > rows) != (b.y > rows)) & (
                cols < a.x + (rows - a.y) * (b.x - a.x) / rise
            )
            inside ^= crosses

    # The half-open ray test is correct for almost all hand coordinates.  Add
    # exact/near boundary centres explicitly so “on the outline” is retained.
    eps2 = 1e-8 ** 2
    for a, b in edges:
        vx, vy = b.x - a.x, b.y - a.y
        length2 = vx * vx + vy * vy
        if length2 <= 1e-18:
            continue
        min_x, max_x = min(a.x, b.x) - 1e-7, max(a.x, b.x) + 1e-7
        min_y, max_y = min(a.y, b.y) - 1e-7, max(a.y, b.y) + 1e-7
        local_x0 = max(0, int(math.floor(min_x - 0.5)) - x0)
        local_x1 = min(cols.shape[1], int(math.ceil(max_x - 0.5)) - x0 + 1)
        local_y0 = max(0, int(math.floor(min_y - 0.5)) - y0)
        local_y1 = min(rows.shape[0], int(math.ceil(max_y - 0.5)) - y0 + 1)
        if local_x1 <= local_x0 or local_y1 <= local_y0:
            continue
        xx = cols[0:1, local_x0:local_x1]
        yy = rows[local_y0:local_y1, 0:1]
        wx = xx - a.x
        wy = yy - a.y
        t = np.clip((wx * vx + wy * vy) / length2, 0.0, 1.0)
        dx = wx - t * vx
        dy = wy - t * vy
        hit = dx * dx + dy * dy <= eps2
        inside[local_y0:local_y1, local_x0:local_x1] |= hit
    return inside


def grain_roi(vertices, dimensions) -> tuple[int, int, int, int]:
    width, height = dimensions
    x0 = max(0, int(math.floor(min(p.x for p in vertices))))
    y0 = max(0, int(math.floor(min(p.y for p in vertices))))
    x1 = min(width, int(math.ceil(max(p.x for p in vertices))))
    y1 = min(height, int(math.ceil(max(p.y for p in vertices))))
    if x1 <= x0 or y1 <= y0:
        raise InputError("矿粒轮廓没有覆盖任何原图范围")
    return x0, y0, x1, y1


def rectangle_patch_in_roi(spec: dict[str, Any], dimensions, roi) -> np.ndarray | None:
    """Rasterize a rotated rectangle only where it intersects the grain ROI."""
    rows, cols, patch = rectangle_slice_mask(field(spec), dimensions)
    x0, y0, x1, y1 = roi
    ix0, iy0 = max(cols.start, x0), max(rows.start, y0)
    ix1, iy1 = min(cols.stop, x1), min(rows.stop, y1)
    if ix0 >= ix1 or iy0 >= iy1:
        return None
    result = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    result[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] = patch[
        iy0 - rows.start:iy1 - rows.start,
        ix0 - cols.start:ix1 - cols.start,
    ]
    return result


def eligible_mask(state: dict[str, Any], grain: dict[str, Any], dimensions) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Return eligible pixel centres in the grain's bounding ROI."""
    # Grains are validated when saved.  Do not re-apply the interactive point
    # bound ``x < width`` here: a geometric boundary may sit on the outer image
    # edge while only pixel centres are tested against the source image.
    vertices = clean_polygon(grain["vertices"])
    x0, y0, x1, y1 = grain_roi(vertices, dimensions)
    outline = polygon_center_mask(vertices, x0, y0, x1, y1)

    field_patch = rectangle_patch_in_roi(state["field"], dimensions, (x0, y0, x1, y1))
    selected = outline if field_patch is None else outline & field_patch
    rejected = np.zeros_like(outline)
    for item in state.get("defects", []):
        patch = rectangle_patch_in_roi(item["shape"], dimensions, (x0, y0, x1, y1))
        if patch is not None:
            rejected |= patch

    eligible = selected & ~rejected
    return eligible, (x0, y0, x1, y1)


def _runs_from_group(xs: np.ndarray, ys: np.ndarray) -> list[list[int]]:
    if xs.size == 0:
        return []
    runs: list[list[int]] = []
    run_y = int(ys[0])
    start = previous = int(xs[0])
    for raw_x, raw_y in zip(xs[1:], ys[1:]):
        x, y = int(raw_x), int(raw_y)
        if y != run_y or x != previous + 1:
            runs.append([run_y, start, previous + 1])
            run_y, start = y, x
        previous = x
    runs.append([run_y, start, previous + 1])
    return runs


def runs_to_pixels(runs: list[list[int]]) -> list[list[int]]:
    return [[x, y] for y, x0, x1 in runs for x in range(x0, x1)]


def runs_to_arrays(runs: list[list[int]]) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for y, x0, x1 in runs:
        xs.extend(range(int(x0), int(x1)))
        ys.extend([int(y)] * (int(x1) - int(x0)))
    return np.asarray(xs, dtype=np.int32), np.asarray(ys, dtype=np.int32)


def _outer_hull(xs: np.ndarray, ys: np.ndarray) -> list[Vec]:
    if cv2 is not None and len(xs) > 64:
        points = np.column_stack((xs, ys)).astype(np.int32)
        hull_index = cv2.convexHull(points, returnPoints=False)
        hull_points = points[hull_index[:, 0]][:, 0, :] if hull_index.ndim == 3 else points[hull_index[:, 0]]
        center_hull = [Vec(float(x) + 0.5, float(y) + 0.5) for x, y in hull_points]
    else:
        center_hull = convex_hull([Vec(float(x) + 0.5, float(y) + 0.5) for x, y in zip(xs, ys)])

    corners: list[Vec] = []
    for point in center_hull:
        # A centre (x + .5, y + .5) owns the cell [x, x+1] × [y, y+1].
        cx, cy = point.x - 0.5, point.y - 0.5
        corners.extend((
            Vec(cx, cy), Vec(cx + 1, cy),
            Vec(cx + 1, cy + 1), Vec(cx, cy + 1),
        ))
    return convex_hull(corners)


def outer_edge_segments(runs: list[list[int]]) -> list[list[int]]:
    """Merge exposed cell edges into maximal integer-coordinate segments."""
    rows: dict[int, list[tuple[int, int]]] = {}
    for y, x0, x1 in runs:
        rows.setdefault(int(y), []).append((int(x0), int(x1)))
    for intervals in rows.values():
        intervals.sort()

    def uncovered(x0: int, x1: int, y: int) -> list[tuple[int, int]]:
        cursor = x0
        pieces: list[tuple[int, int]] = []
        for a, b in rows.get(y, ()):  # type: ignore[arg-type]
            if b <= cursor or a >= x1:
                if a >= x1:
                    break
                continue
            if a > cursor:
                pieces.append((cursor, min(a, x1)))
            cursor = max(cursor, a)
            if cursor >= x1:
                break
        if cursor < x1:
            pieces.append((cursor, x1))
        return [(a, b) for a, b in pieces if b > a]

    edges: list[list[int]] = []
    for y, x0, x1 in runs:
        y, x0, x1 = int(y), int(x0), int(x1)
        for a, b in uncovered(x0, x1, y - 1):
            edges.append([a, y, b, y])
        for a, b in uncovered(x0, x1, y + 1):
            edges.append([a, y + 1, b, y + 1])
        edges.append([x0, y, x0, y + 1])
        edges.append([x1, y, x1, y + 1])

    # Merge collinear unit segments produced by adjacent runs/rows.  The input
    # is small for ordinary annotations and keeps the persisted overlay tidy.
    horizontal: dict[tuple[int, int], list[int]] = {}
    vertical: dict[tuple[int, int], list[int]] = {}
    for x1, y1, x2, y2 in edges:
        if y1 == y2:
            horizontal.setdefault((y1, 1 if x2 >= x1 else -1), []).extend([min(x1, x2), max(x1, x2)])
        else:
            vertical.setdefault((x1, 1 if y2 >= y1 else -1), []).extend([min(y1, y2), max(y1, y2)])

    merged: list[list[int]] = []
    for (y, direction), values in horizontal.items():
        xs = sorted(set(values))
        start = previous = xs[0]
        for value in xs[1:]:
            if value > previous + 1:
                merged.append([start, y, previous, y])
                start = value
            previous = value
        merged.append([start, y, previous, y])
    for (x, direction), values in vertical.items():
        ys = sorted(set(values))
        start = previous = ys[0]
        for value in ys[1:]:
            if value > previous + 1:
                merged.append([x, start, x, previous])
                start = value
            previous = value
        merged.append([x, start, x, previous])
    return merged


def section_measurements(xs: np.ndarray, ys: np.ndarray, manual_axis_deg: float) -> dict[str, Any]:
    area = int(xs.size)
    hull = _outer_hull(xs, ys)
    enclosing = None
    if len(hull) >= 3:
        feret_max, feret_min, caliper_axis, enclosing = caliper_geometry(hull)
    else:  # Defensive: a valid cell set always has a four-corner hull.
        x0, x1 = float(xs.min()), float(xs.max() + 1)
        y0, y1 = float(ys.min()), float(ys.max() + 1)
        feret_max = math.hypot(x1 - x0, y1 - y0)
        feret_min = min(x1 - x0, y1 - y0)
        caliper_axis = manual_axis_deg

    theta = math.radians(manual_axis_deg)
    ux, uy = math.cos(theta), math.sin(theta)
    projection_u = ux * (xs.astype(np.float64) + 0.5) + uy * (ys.astype(np.float64) + 0.5)
    projection_v = -uy * (xs.astype(np.float64) + 0.5) + ux * (ys.astype(np.float64) + 0.5)
    cell_support = abs(ux) + abs(uy)
    manual_center_span = float(projection_u.max() - projection_u.min())
    manual_span = manual_center_span + cell_support
    perpendicular_center_span = float(projection_v.max() - projection_v.min())
    perpendicular_span = perpendicular_center_span + cell_support

    xmin, xmax = int(xs.min()), int(xs.max())
    ymin, ymax = int(ys.min()), int(ys.max())
    bbox_width = xmax - xmin + 1
    bbox_height = ymax - ymin + 1
    bbox_long = max(bbox_width, bbox_height)
    bbox_short = min(bbox_width, bbox_height)
    if enclosing:
        oriented_width = float(enclosing["width"])
        oriented_height = float(enclosing["height"])
        oriented_long = max(oriented_width, oriented_height)
        oriented_short = min(oriented_width, oriented_height)
    else:
        oriented_width, oriented_height = float(bbox_width), float(bbox_height)
        oriented_long, oriented_short = float(bbox_long), float(bbox_short)

    return {
        "area_px2": area,
        "pixel_count": area,
        "pixels_area_px2": area,
        # True rotating-caliper/Feret values on the outer corners of cells.
        "caliper_max_px": feret_max,
        "caliper_min_px": feret_min,
        "long_caliper_px": feret_max,
        "short_caliper_px": feret_min,
        "longest_caliper_px": feret_max,
        "shortest_caliper_px": feret_min,
        "feret_max_px": feret_max,
        "feret_min_px": feret_min,
        "caliper_axis_deg": caliper_axis,
        # Extent in the operator's original hand-traced long direction.
        "manual_axis_deg": float(manual_axis_deg),
        "axis_deg": float(manual_axis_deg),
        "manual_span_px": manual_span,
        "axis_span_px": manual_span,
        "longitudinal_span_px": manual_span,
        "outer_span_px": manual_span,
        "manual_center_span_px": manual_center_span,
        "center_span_px": manual_center_span,
        "perpendicular_span_px": perpendicular_span,
        "perpendicular_center_span_px": perpendicular_center_span,
        # Simple axis-aligned outer grid box.
        "bbox": {
            "x": xmin, "y": ymin,
            "width": bbox_width, "height": bbox_height,
            "right": xmax + 1, "bottom": ymax + 1,
        },
        "bbox_width_px": bbox_width,
        "bbox_height_px": bbox_height,
        "bbox_long_px": bbox_long,
        "bbox_short_px": bbox_short,
        "oriented_width_px": oriented_width,
        "oriented_height_px": oriented_height,
        "oriented_long_px": oriented_long,
        "oriented_short_px": oriented_short,
        # Orthogonal outer rectangle aliases for callers that use “caliper”
        # for long/short bounding sides rather than maximum/minimum Feret.
        "side_long_caliper_px": oriented_long,
        "side_short_caliper_px": oriented_short,
        "grid_long_caliper_px": float(bbox_long),
        "grid_short_caliper_px": float(bbox_short),
    }


def _candidate_from_group(index: int, xs: np.ndarray, ys: np.ndarray, manual_axis_deg: float,
                          offset: tuple[int, int] | None = None) -> dict[str, Any]:
    if offset is not None:
        x0, y0 = offset
        xs = xs + x0
        ys = ys + y0
    runs = _runs_from_group(xs, ys)
    result: dict[str, Any] = {
        "version": SECTION_VERSION,
        "component_index": index,
        "four_connected": True,
        "runs": runs,
        "edges": outer_edge_segments(runs),
    }
    result.update(section_measurements(xs, ys, manual_axis_deg))
    return result


def recommend_section(state: dict[str, Any], grain: dict[str, Any], dimensions,
                      component_index: int | None = None) -> dict[str, Any]:
    """Select one four-connected eligible component.

    The recommendation is the best balanced normalized area/long-span score.
    Pass ``component_index`` to deterministically choose another fragment.
    """
    eligible, roi = eligible_mask(state, grain, dimensions)
    if not eligible.any():
        raise InputError("视域、原圈和废区条件的交集中没有可采用的像素中心")

    labels, count = ndimage.label(eligible, structure=FOUR_NEIGHBOUR)
    if count <= 0:
        raise InputError("没有上下左右相接的可采用截面")

    manual_axis = grain_metrics(grain["vertices"])["long_axis_deg"]
    roi_x0, roi_y0, _, _ = roi
    ys, xs = np.nonzero(eligible)
    component_ids = labels[ys, xs]
    order = np.argsort(component_ids, kind="stable")
    component_ids = component_ids[order]
    xs = xs[order] + roi_x0
    ys = ys[order] + roi_y0
    unique, starts, counts = np.unique(component_ids, return_index=True, return_counts=True)

    candidates: list[dict[str, Any]] = []
    for label, start, group_size in zip(unique, starts, counts):
        group_size = int(group_size)
        start = int(start)
        candidate = _candidate_from_group(
            int(label), xs[start:start + group_size], ys[start:start + group_size], manual_axis,
        )
        candidates.append(candidate)

    max_area = max(item["area_px2"] for item in candidates)
    max_span = max(item["manual_span_px"] for item in candidates)
    for item in candidates:
        area_score = item["area_px2"] / max_area
        span_score = item["manual_span_px"] / max_span if max_span else 0.0
        item["area_score"] = area_score
        item["span_score"] = span_score
        item["score"] = math.sqrt(area_score * span_score)

    def rank(item):
        return (
            item["score"], item["area_px2"], item["manual_span_px"],
            -item["bbox"]["y"], -item["bbox"]["x"], -item["component_index"],
        )

    candidates.sort(key=rank, reverse=True)
    if component_index is not None:
        selected = next((item for item in candidates if item["component_index"] == component_index), None)
        if selected is None:
            raise InputError("指定的截面残片不存在")
    else:
        selected = candidates[0]
    selected["recommended"] = True
    selected["candidate_count"] = len(candidates)
    return {
        "grain_id": grain.get("id"),
        "grain_name": grain.get("name"),
        "manual_axis_deg": manual_axis,
        "axis_deg": manual_axis,
        "component_count": len(candidates),
        "candidates": candidates,
        "components": candidates,
        "recommended": selected,
        "section": selected,
        "best": selected,
    }


def select_section(state: dict[str, Any], grain: dict[str, Any], dimensions,
                   component_index: int | None = None) -> dict[str, Any]:
    """Return just the recommended/selected section record."""
    return recommend_section(state, grain, dimensions, component_index)["recommended"]


def recommended_section(state, grain, dimensions, component_index=None):
    """Compatibility alias for callers naming the saved block directly."""
    return select_section(state, grain, dimensions, component_index)


def choose_section(state, grain, dimensions, component_index=None):
    """Verb-first compatibility alias."""
    return select_section(state, grain, dimensions, component_index)


def with_physical_units(section: dict[str, Any], um_per_px: float | None) -> dict[str, Any]:
    """Copy a section and add current-scale physical measurements."""
    result = dict(section)
    if um_per_px is None:
        for key in list(result):
            if key.endswith("_um"):
                result[key] = None
        result["scale_um_per_px"] = None
        return result

    factor = float(um_per_px)
    result["scale_um_per_px"] = factor
    result["area_um2"] = result["area_px2"] * factor * factor
    physical_keys = (
        "caliper_max", "caliper_min", "long_caliper", "short_caliper",
        "longest_caliper", "shortest_caliper", "feret_max", "feret_min",
        "manual_span", "axis_span", "longitudinal_span", "perpendicular_span",
        "manual_center_span", "center_span", "perpendicular_center_span", "outer_span",
        "bbox_long", "bbox_short", "bbox_width", "bbox_height",
        "oriented_width", "oriented_height", "oriented_long", "oriented_short",
        "side_long_caliper", "side_short_caliper", "grid_long_caliper", "grid_short_caliper",
    )
    for key in physical_keys:
        px_key = key + "_px"
        if px_key in result:
            result[key + "_um"] = result[px_key] * factor
    return result


def clean_section(value: Any, dimensions) -> dict[str, Any]:
    """Validate an imported/persisted section and recompute its measurements."""
    if not isinstance(value, dict):
        raise InputError("采用截面必须是对象")
    runs_raw = value.get("runs", value.get("mask_runs"))
    if runs_raw is None and isinstance(value.get("pixels"), list):
        by_row: dict[int, list[int]] = {}
        for pixel in value["pixels"]:
            if isinstance(pixel, dict):
                px, py = pixel.get("x"), pixel.get("y")
            elif isinstance(pixel, (list, tuple)) and len(pixel) == 2:
                px, py = pixel
            else:
                raise InputError("采用截面像素坐标无效")
            if isinstance(px, bool) or isinstance(py, bool) or not isinstance(px, int) or not isinstance(py, int):
                raise InputError("采用截面像素坐标应为整数")
            by_row.setdefault(py, []).append(px)
        runs_raw = []
        for py in sorted(by_row):
            xs = sorted(set(by_row[py]))
            start = previous = xs[0]
            for px in xs[1:]:
                if px != previous + 1:
                    runs_raw.append([py, start, previous + 1])
                    start = px
                previous = px
            runs_raw.append([py, start, previous + 1])
    if not isinstance(runs_raw, list) or not 1 <= len(runs_raw) <= 500_000:
        raise InputError("采用截面的像素行程无效")
    width, height = dimensions
    runs: list[list[int]] = []
    seen: set[tuple[int, int]] = set()
    for run in runs_raw:
        if not isinstance(run, (list, tuple)) or len(run) != 3:
            raise InputError("采用截面行程应为 [y,x_start,x_end]")
        y, x0, x1 = run
        if (
            isinstance(y, bool) or isinstance(x0, bool) or isinstance(x1, bool)
            or not all(isinstance(v, int) for v in run)
        ):
            raise InputError("采用截面坐标应为整数")
        if not 0 <= y < height or not 0 <= x0 < x1 <= width:
            raise InputError("采用截面行程超出原图")
        for x in range(x0, x1):
            key = (x, y)
            if key in seen:
                raise InputError("采用截面行程存在重复像素")
            seen.add(key)
        runs.append([int(y), int(x0), int(x1)])

    # Ensure the persisted cells really are one four-connected component.
    mask = np.zeros((height, width), dtype=bool)
    for y, x0, x1 in runs:
        mask[y, x0:x1] = True
    labels, count = ndimage.label(mask, structure=FOUR_NEIGHBOUR)
    if count != 1:
        raise InputError("采用截面必须是上下左右相接的一块")

    xs, ys = runs_to_arrays(runs)
    axis = value.get("manual_axis_deg", value.get("axis_deg", 0.0))
    if isinstance(axis, bool) or not isinstance(axis, (int, float)) or not math.isfinite(axis):
        raise InputError("采用截面长轴方向无效")
    axis = float(axis) % 180
    result: dict[str, Any] = {
        "version": SECTION_VERSION,
        "component_index": 1,
        "four_connected": True,
        "recommended": bool(value.get("recommended", True)),
        "runs": runs,
        "edges": outer_edge_segments(runs),
    }
    result.update(section_measurements(xs, ys, axis))
    if isinstance(value.get("candidate_count"), int):
        result["candidate_count"] = max(1, int(value["candidate_count"]))
    return result
