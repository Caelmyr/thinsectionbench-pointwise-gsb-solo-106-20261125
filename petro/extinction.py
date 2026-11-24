"""Extinction directions from a recorded stage-angle rotation series.

Only frames taken at recorded stage angles are compared.  A dark valley is a
frame that is darker inside the traced grain than the nearest recorded frame on
each side; the two bracketing frames must be at most ``max_bracket`` degrees
apart and the sequence is never joined end to end.  Stage angles repeat their
optical direction every 90 degrees, so folded valleys can corroborate one
another.  Brightness readings for a valley use only pixels that are not pure
white in all three of its frames.
"""
from __future__ import annotations

import math

import numpy as np

from .grain_optics import grain_window
from .raster import luminance

FOLD_PERIOD = 90.0
TOLERANCE = 3.0
MIN_FRAMES = 3
PURE_WHITE = 255


def circular_gap(first, second, period=FOLD_PERIOD):
    delta = abs(float(first) - float(second)) % period
    return min(delta, period - delta)


def _mean(values, mask):
    count = int(np.count_nonzero(mask))
    if count == 0:
        return None, 0
    return float(np.mean(values[mask], dtype=np.float64)), count


def _frame_report(item, values, white, valid):
    usable = valid & ~white
    mean, count = _mean(values, usable)
    return {
        "id": item["id"],
        "name": item["name"],
        "angle": float(item["angle"]),
        "sampled_pixels": count,
        "white_pixels": int(np.count_nonzero(valid & white)),
        "mean": mean,
    }


def _support_summary(direction, group):
    total = sum(item["depth"] for item in group)
    rounds = len({item["round"] for item in group})
    spread = max(circular_gap(item["folded_deg"], direction) for item in group)
    shares = sorted(
        ({key: item[key] for key in ("frame_id", "name", "angle", "folded_deg", "round",
                                     "left", "right", "bracket_deg", "pixels",
                                     "left_mean", "valley_mean", "right_mean", "depth")}
         for item in group),
        key=lambda item: (item["angle"], item["frame_id"]),
    )
    return {
        "direction_deg": direction,
        "score": total,
        "support_count": len(group),
        "rounds": rounds,
        "max_depth": max(item["depth"] for item in group),
        "darkest_mean": min(item["valley_mean"] for item in group),
        "spread_deg": spread,
        "supports": shares,
    }


def _choose_direction(accepted):
    """Prefer the direction with the deepest combined evidence and most rounds."""
    best = None
    for candidate in accepted:
        direction = candidate["folded_deg"]
        group = [
            item for item in accepted
            if circular_gap(item["folded_deg"], direction) <= TOLERANCE + 1e-9
        ]
        summary = _support_summary(direction, group)
        key = (
            round(summary["score"], 9),
            summary["rounds"],
            summary["support_count"],
            round(-summary["spread_deg"], 9),
            round(-summary["darkest_mean"], 9),
            round(-direction, 9),
        )
        if best is None or key > best[0]:
            best = (key, summary)
    return best[1]


def _insufficient_reason(ordered, valleys, accepted, max_bracket):
    if not ordered:
        return "还没有带台角的偏光序列"
    if len(ordered) < MIN_FRAMES:
        return f"带台角的照片只有 {len(ordered)} 张，至少需要三张且首尾不互相连接"
    if not valleys:
        return "按台角排序后没有两侧都有实拍照片、且三张都有未纯白像素的亮度暗谷"
    if not accepted:
        return f"找到的暗谷都不满足夹照间隔不超过 {max_bracket:g}° 或三张共有像素的条件"
    return None


def extinction_direction(items, loader, state, grain, max_bracket):
    """Scan the recorded stage angles and recommend one folded extinction direction."""
    ordered = sorted(items, key=lambda item: (float(item["angle"]), item["id"]))
    report = {
        "grain": {"id": grain["id"], "name": grain["name"], "mineral": grain["mineral"]},
        "frame_count": len(ordered),
        "stage_span_deg": (
            float(ordered[-1]["angle"]) - float(ordered[0]["angle"]) if len(ordered) >= 2 else 0.0
        ),
        "max_bracket_deg": float(max_bracket),
        "fold_period_deg": FOLD_PERIOD,
        "tolerance_deg": TOLERANCE,
        "frames": [],
        "valleys": [],
        "other_valleys": [],
        "recommended": None,
        "insufficient": None,
    }
    if not ordered:
        report["insufficient"] = _insufficient_reason(ordered, [], [], max_bracket)
        return report

    window = grain_window(loader(ordered[0]), state, grain)
    report["grain"]["outline_pixels"] = int(np.count_nonzero(window.outline))
    report["grain"]["usable_pixels"] = int(np.count_nonzero(window.valid))
    if not window.valid.any():
        report["insufficient"] = "矿粒在观察视域和废区之外没有可比像素"
        return report

    values = []
    whites = []
    for item in ordered:
        patch = loader(item)[window.rows, window.cols]
        values.append(luminance(patch))
        whites.append(np.all(patch >= PURE_WHITE, axis=-1))
        report["frames"].append(_frame_report(item, values[-1], whites[-1], window.valid))

    valleys = []
    accepted = []
    for index in range(1, len(ordered) - 1):
        middle = report["frames"][index]
        before = report["frames"][index - 1]
        after = report["frames"][index + 1]
        gap = after["angle"] - before["angle"]
        entry = {
            "frame_id": middle["id"],
            "name": middle["name"],
            "angle": middle["angle"],
            "folded_deg": middle["angle"] % FOLD_PERIOD,
            "round": int(math.floor(middle["angle"] / FOLD_PERIOD)),
            "left": {"id": before["id"], "name": before["name"], "angle": before["angle"]},
            "right": {"id": after["id"], "name": after["name"], "angle": after["angle"]},
            "bracket_deg": gap,
            "pixels": 0,
            "left_mean": None,
            "valley_mean": None,
            "right_mean": None,
            "depth": None,
            "status": "rejected",
            "reason": None,
        }
        if gap > max_bracket + 1e-9:
            entry["reason"] = f"左右台角间隔 {gap:g}° 超过 {max_bracket:g}°"
            valleys.append(entry)
            continue
        if middle["mean"] is None or before["mean"] is None or after["mean"] is None:
            entry["reason"] = "矿粒内没有未饱和可比像素"
            valleys.append(entry)
            continue
        if not (middle["mean"] < before["mean"] and middle["mean"] < after["mean"]):
            entry["reason"] = "谷底在这个台角不是三段中最暗"
            valleys.append(entry)
            continue
        common = window.valid & ~whites[index - 1] & ~whites[index] & ~whites[index + 1]
        left_mean, pixels = _mean(values[index - 1], common)
        valley_mean, _ = _mean(values[index], common)
        right_mean, _ = _mean(values[index + 1], common)
        entry.update({
            "pixels": pixels,
            "left_mean": left_mean,
            "valley_mean": valley_mean,
            "right_mean": right_mean,
        })
        if pixels == 0:
            entry["reason"] = "三张共有的未纯白像素为空"
            valleys.append(entry)
            continue
        if valley_mean >= min(left_mean, right_mean):
            entry["reason"] = "三张共有像素下不是暗谷"
            valleys.append(entry)
            continue
        entry["depth"] = min(left_mean, right_mean) - valley_mean
        entry["status"] = "other"
        valleys.append(entry)
        accepted.append(entry)

    reason = _insufficient_reason(ordered, valleys, accepted, max_bracket)
    report["valleys"] = valleys
    if accepted:
        best = _choose_direction(accepted)
        for entry in accepted:
            entry["status"] = "rejected"
            entry["reason"] = "方向不在推荐方向 ±%g° 周期互证内" % TOLERANCE
        for support in best["supports"]:
            entry = next(item for item in accepted if item["frame_id"] == support["frame_id"])
            entry["status"] = "supported"
            entry["reason"] = None
            support["status"] = "supported"
        report["other_valleys"] = [entry for entry in valleys if entry["status"] == "rejected"]
        report["recommended"] = best
        reason = None
    report["insufficient"] = reason
    return report