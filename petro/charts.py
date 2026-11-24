"""Pixel-precise chart rendering for offline observation plates."""
from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .markups import BORDER, DIM, INK, MINT, PALE, WARM, Rect, fitted_text, font, set_panel

CYAN = "#87c9d8"
SALMON = "#e49991"
GRID = "#304850"
CHART_BG = "#11232d"


@dataclass(frozen=True)
class Range:
    lower: float
    upper: float

    def fraction(self, value):
        return (value - self.lower) / (self.upper - self.lower)

    def clip(self, value):
        return min(self.upper, max(self.lower, value))


@dataclass(frozen=True)
class Plot:
    extent: Rect
    horizontal: Range
    vertical: Range

    def point(self, x, y):
        return (
            self.extent.x + self.horizontal.fraction(x) * self.extent.w,
            self.extent.bottom() - self.vertical.fraction(y) * self.extent.h,
        )


def _numeric(value):
    if value is None:
        return "n/a"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 5:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _digits_for_step(value):
    return max(0, 1 - math.floor(math.log10(value)))


def nice_step(span, tick_count):
    approximate = span / max(1, tick_count)
    magnitude = 10 ** math.floor(math.log10(approximate))
    ratio = approximate / magnitude
    for stride in (1, 2, 2.5, 5, 10):
        if ratio <= stride * 1.001:
            return stride * magnitude
    return 10 * magnitude


def extent(values, include_zero=False, padding=0.05):
    valid = [float(value) for value in values if value is not None and math.isfinite(value)]
    if not valid:
        return Range(0, 1)
    minimum = min(valid)
    maximum = max(valid)
    if include_zero:
        minimum = min(0, minimum)
        maximum = max(0, maximum)
    if math.isclose(minimum, maximum, abs_tol=1e-10):
        return Range(minimum - 0.5, maximum + 0.5)
    pad = (maximum - minimum) * padding
    if minimum != 0 or not include_zero:
        minimum -= pad
    if maximum != 0 or not include_zero:
        maximum += pad
    return Range(minimum, maximum)


def tick_positions(interval, limit=5):
    step = nice_step(interval.upper - interval.lower, limit)
    start = math.ceil(interval.lower / step - 0.00001)
    stop = math.floor(interval.upper / step + 0.00001)
    digits = _digits_for_step(step)
    return [
        (i * step, f"{i * step:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{i * step:.0f}")
        for i in range(start, stop + 1)
    ]


def _plot_panel(draw, target, title, subtitle=None):
    set_panel(draw, target.bounds)
    fitted_text(draw, (target.x + 16, target.y + 13), title, target.w - 32, 20)
    if subtitle:
        fitted_text(draw, (target.x + 16, target.y + 42), subtitle, target.w - 32, 13, DIM)


def plot_frame(draw, target, horizontal, vertical, caption_x, caption_y, subheader=False):
    top = target.y + (75 if subheader else 52)
    region = Rect(target.x + 64, top, target.w - 89, target.bottom() - top - 51)
    plot = Plot(region, horizontal, vertical)
    draw.rectangle(region.bounds, fill=CHART_BG, outline=BORDER)
    for value, text in tick_positions(horizontal, max(2, target.w // 100)):
        at, _ = plot.point(value, vertical.lower)
        draw.line((at, region.y, at, region.bottom()), fill=GRID)
        fitted_text(draw, (at, region.bottom() + 8), text, 55, 12, DIM, anchor="ma")
    for value, text in tick_positions(vertical, 5):
        _, at = plot.point(horizontal.lower, value)
        draw.line((region.x, at, region.right(), at), fill=GRID)
        fitted_text(draw, (region.x - 7, at), text, 49, 12, DIM, anchor="rm")
    fitted_text(draw, (region.right(), target.bottom() - 20), caption_x, region.w, 13, DIM, anchor="ra")
    fitted_text(draw, (region.x, region.y - 17), caption_y, region.w, 12, DIM)
    return plot


def _polyline(draw, plot, samples, color, width=2, markers=False):
    path = []
    for x, y in samples:
        if y is None:
            if len(path) > 1:
                draw.line(path, fill=color, width=width)
            elif len(path) == 1:
                _dot(draw, path[0], color, 2)
            path = []
            continue
        scaled = plot.point(plot.horizontal.clip(x), plot.vertical.clip(y))
        path.append(scaled)
        if markers:
            _dot(draw, scaled, color, 3)
    if len(path) > 1:
        draw.line(path, fill=color, width=width)
    elif len(path) == 1:
        _dot(draw, path[0], color, 2)


def _dot(draw, point, color, radius):
    x, y = point
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def _bands(draw, plot, samples):
    if not samples:
        return
    intervals = []
    start = None
    for index, item in enumerate(samples):
        if item[1] is None and start is None:
            start = index
        elif item[1] is not None and start is not None:
            intervals.append((start, index - 1))
            start = None
    if start is not None:
        intervals.append((start, len(samples) - 1))
    for a, b in intervals:
        start_x = samples[a][0] if a == 0 else (samples[a - 1][0] + samples[a][0]) / 2
        end_x = samples[b][0] if b == len(samples) - 1 else (samples[b][0] + samples[b + 1][0]) / 2
        left, _ = plot.point(start_x, 0)
        right, _ = plot.point(end_x, 0)
        draw.rectangle((left, plot.extent.y + 1, right, plot.extent.bottom() - 1), fill="#4b3438")


def histogram_chart(surface, target, data):
    draw = ImageDraw.Draw(surface)
    line = f"Usable source pixels: {data['pixels']:,d}  |  median {_numeric(data['statistics']['median'])}"
    _plot_panel(draw, target, "Field histogram", line)
    values = data["histogram"]
    yrange = Range(0, max(1, max(row["count"] for row in values)) * 1.10)
    plot = plot_frame(draw, target, Range(0, 256), yrange, "8-bit levels", "pixels", True)
    for row in values:
        if row["count"] == 0:
            continue
        left, bottom = plot.point(row["from"], 0)
        right, top = plot.point(row["to"], row["count"])
        # Small nonzero bins can round to the baseline. Give them one pixel.
        bar_bottom = bottom - 1
        draw.rectangle(
            (left + 1, min(top, bar_bottom), max(left + 1, right - 1), bar_bottom),
            fill=MINT,
        )


def profile_chart(surface, target, data):
    draw = ImageDraw.Draw(surface)
    span = f"{data['valid_samples']} / {len(data['samples'])} usable samples"
    _plot_panel(draw, target, "Transect intensity", span)
    unit = "um" if data["length_um"] is not None else "px"
    key = "distance_um" if unit == "um" else "distance_px"
    length = data["length_um"] if unit == "um" else data["length_px"]
    plot = plot_frame(draw, target, Range(0, max(1e-6, length)), Range(0, 255), unit, "8-bit levels", True)
    samples = [(item[key], item["value"]) for item in data["samples"]]
    _bands(draw, plot, samples)
    _polyline(draw, plot, samples, WARM, width=2)


def sequence_chart(surface, target, data):
    draw = ImageDraw.Draw(surface)
    interpretation = f"{len(data['values'])} measured frame(s)  |  {data['pixels_per_frame']:,d} pixels per frame"
    _plot_panel(draw, target, "Rotation sequence", interpretation)
    aligned = data["values"]
    plot = plot_frame(
        draw, target, extent([item["angle"] for item in aligned], include_zero=True, padding=0.03),
        Range(0, 255), "stage degrees", "8-bit levels", True,
    )
    for item in aligned:
        angle = item["angle"]
        lo = plot.point(angle, item["samples"]["p05"])
        hi = plot.point(angle, item["samples"]["p95"])
        draw.line((lo, hi), fill=CYAN, width=2)
        for x, y in (lo, hi):
            draw.line((x - 4, y, x + 4, y), fill=CYAN, width=2)
    _polyline(draw, plot, [(row["angle"], row["samples"]["mean"]) for row in aligned], MINT, width=2, markers=True)


def result_notice(surface, target, heading, explanation):
    draw = ImageDraw.Draw(surface)
    _plot_panel(draw, target, heading)
    fitted_text(draw, (target.x + 16, target.y + 65), explanation, target.w - 32, 15, DIM)


def rose_chart(surface, target, values, weight, heading, strength):
    draw = ImageDraw.Draw(surface)
    _plot_panel(draw, target, heading)
    radius = min(target.w * 0.34, (target.h - 65) * 0.40)
    center = (target.x + target.w / 2, target.y + 52 + (target.h - 52) / 2)
    cx, cy = center
    for portion in (0.25, 0.5, 0.75, 1):
        inner = radius * portion
        draw.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), outline=BORDER, width=1)
    for heading in (0, 30, 60, 90, 120, 150):
        theta = math.radians(heading)
        dx = radius * math.cos(theta)
        dy = radius * math.sin(theta)
        draw.line((cx - dx, cy - dy, cx + dx, cy + dy), fill=BORDER, width=1)
    top = max((item[weight] for item in values), default=0)
    if top > 0:
        for item in values:
            distance = radius * item[weight] / top
            if distance <= 0.1:
                continue
            bounds = (cx - distance, cy - distance, cx + distance, cy + distance)
            for offset in (0, 180):
                draw.pieslice(bounds, item["from"] + offset + 0.5, item["to"] + offset - 0.5, fill=MINT)
    fitted_text(draw, (cx + radius + 5, cy), "0 deg", 45, 12, DIM, anchor="lm")
    fitted_text(draw, (cx, cy + radius + 5), "90 deg", 55, 12, DIM, anchor="ma")
    fitted_text(draw, (target.x + 16, target.bottom() - 28), f"Resultant: {_numeric(strength)}", target.w - 30, 13, DIM)


def tally_chart(surface, target, record, labels):
    draw = ImageDraw.Draw(surface)
    unit = "of manually recorded points"
    _plot_panel(draw, target, "Manual point records", f"{record['count']} points  |  percentages {unit}")
    entries = record["minerals"]
    if not entries:
        return
    available = target.h - 91
    row_h = max(16, available // len(entries))
    start_x = target.x + 137
    right_x = target.right() - 47
    for i, item in enumerate(entries):
        y = target.y + 69 + i * row_h
        label = labels[item["key"]]
        fitted_text(draw, (target.x + 17, y + 2), label, 110, 13, DIM)
        draw.rounded_rectangle((start_x, y + 5, right_x, y + 15), radius=4, fill=CHART_BG)
        if item["percent"] is not None and item["count"]:
            count = max(2, float(item["percent"]) / 100 * (right_x - start_x))
            draw.rounded_rectangle((start_x, y + 5, start_x + count, y + 15), radius=4, fill=item["color"])
        fitted_text(draw, (target.right() - 15, y + 1), str(item["count"]), 29, 13, PALE, anchor="ra")


def full_chart(width, height, callback, *args):
    surface = Image.new("RGB", (width, height), INK)
    callback(surface, Rect(16, 16, width - 32, height - 32), *args)
    return surface
