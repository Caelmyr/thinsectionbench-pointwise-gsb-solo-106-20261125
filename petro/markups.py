"""Raster plate composition for visual inspection and printable evidence."""
from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .domain import PALETTE, Vec, field

PALE = "#e7eee9"
DIM = "#9bafaf"
MINT = "#a7dfbf"
WARM = "#e9c987"
INK = "#132028"
PANEL = "#172a34"
BORDER = "#354e56"
DEFECT_COLOR = "#e38d85"
LABS = {
    "quartz": "Quartz",
    "feldspar": "Feldspar",
    "mica": "Mica",
    "amphibole": "Amphibole",
    "opaque": "Opaque",
    "other": "Other mineral",
    "pore": "Pore",
    "undetermined": "Unreadable",
}
DEFECT_NAMES = {
    "bubble": "Bubble",
    "fracture": "Mount fracture",
    "resin": "Resin edge",
    "blur": "Blur",
    "dust": "Debris",
}


def printable(text):
    return "".join(char if 32 <= ord(char) < 127 else " " for char in str(text)).strip()


def font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def fitted_text(draw, xy, text, width, size=15, fill=PALE, anchor=None):
    typeface = font(size)
    text = str(text)
    if draw.textlength(text, font=typeface) > width:
        low = 0
        high = len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if draw.textlength(text[:middle] + "...", font=typeface) <= width:
                low = middle
            else:
                high = middle - 1
        text = text[:low] + "..."
    draw.text(xy, text, font=typeface, fill=fill, anchor=anchor)


def set_panel(draw, box, fill=PANEL):
    draw.rounded_rectangle(box, radius=10, fill=fill, outline=BORDER, width=1)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def bounds(self):
        return self.x, self.y, self.x + self.w, self.y + self.h

    def pad(self, pixels):
        return Rect(self.x + pixels, self.y + pixels, self.w - pixels * 2, self.h - pixels * 2)

    def right(self):
        return self.x + self.w

    def bottom(self):
        return self.y + self.h


@dataclass(frozen=True)
class Mapping:
    data_width: float
    data_height: float
    destination: Rect
    rotated_field: object | None = None

    def coords(self, xy):
        vector = Vec(xy["x"], xy["y"]) if isinstance(xy, dict) else xy
        if self.rotated_field is not None:
            vector = self.rotated_field.to_local(vector)
        return (
            self.destination.x + (vector.x / self.data_width) * self.destination.w,
            self.destination.y + (vector.y / self.data_height) * self.destination.h,
        )

    def visible(self, xy):
        x, y = xy
        return self.destination.x <= x <= self.destination.right() and self.destination.y <= y <= self.destination.bottom()


@dataclass(frozen=True)
class Annotate:
    field: bool = True
    defects: bool = True
    points: bool = True
    grains: bool = True
    guide: bool = False
    line: bool = True


def fitted_rect(source, destination):
    input_width, input_height = source.size
    factor = min(destination.w / input_width, destination.h / input_height)
    width = max(1, int(round(input_width * factor)))
    height = max(1, int(round(input_height * factor)))
    left = destination.x + (destination.w - width) // 2
    top = destination.y + (destination.h - height) // 2
    return Rect(left, top, width, height)


def paste_image(target, source, destination, nearest=False):
    actual = fitted_rect(source, destination)
    resampling = Image.Resampling.NEAREST if nearest else Image.Resampling.LANCZOS
    scaled = source.resize((actual.w, actual.h), resampling)
    target.paste(scaled, (actual.x, actual.y))
    return actual


def _dash_segment(draw, start, end, fill, dash=7, gap=4, width=2):
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance <= 0.05:
        return
    if distance > 3000:
        dash = max(dash, distance / 430)
    phase = 0.0
    while phase < distance:
        end_phase = min(distance, phase + dash)
        sa = phase / distance
        sb = end_phase / distance
        draw.line(
            (
                (start[0] + dx * sa, start[1] + dy * sa),
                (start[0] + dx * sb, start[1] + dy * sb),
            ),
            fill=fill,
            width=width,
        )
        phase += dash + gap


def _clipped_segment(start, end, rect):
    x0, y0 = start
    dx, dy = end[0] - x0, end[1] - y0
    lower = 0.0
    upper = 1.0
    for edge, distance in zip(
        (-dx, dx, -dy, dy),
        (x0 - rect.x, rect.right() - x0, y0 - rect.y, rect.bottom() - y0),
    ):
        if abs(edge) < 1e-12:
            if distance < 0:
                return None
            continue
        intersection = distance / edge
        if edge < 0:
            lower = max(lower, intersection)
        else:
            upper = min(upper, intersection)
        if lower > upper:
            return None
    return (x0 + dx * lower, y0 + dy * lower), (x0 + dx * upper, y0 + dy * upper)


def _outline(draw, coords, target, fill, dashed=False, width=2):
    for a, b in zip(coords, coords[1:] + coords[:1]):
        section = _clipped_segment(a, b, target)
        if section is None:
            continue
        if dashed:
            _dash_segment(draw, *section, fill, width=width)
        else:
            draw.line(section, fill=fill, width=width)


def _line(draw, first, last, target, color, dashed=False, width=2):
    clipped = _clipped_segment(first, last, target)
    if clipped is None:
        return
    if dashed:
        _dash_segment(draw, *clipped, color, width=width)
    else:
        draw.line(clipped, fill=color, width=width)


def _style_mark(draw, xy, fill, radius, outlined=True):
    x, y = xy
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=fill,
        outline=INK if outlined else None,
        width=2 if outlined else 1,
    )


def _text_bounds(draw, x, y, text, size):
    a, b, c, d = draw.textbbox((x, y), text, font=font(size))
    return a - 4, b - 4, c + 4, d + 4


def _intersects(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


class Labels:
    def __init__(self, draw, available):
        self.draw = draw
        self.available = available
        self.used = []

    def _available_at(self, x, y, text, size):
        bounds = _text_bounds(self.draw, x, y, text, size)
        p = self.available
        if bounds[0] < p.x or bounds[1] < p.y or bounds[2] > p.right() or bounds[3] > p.bottom():
            return None
        if any(_intersects(bounds, busy) for busy in self.used):
            return None
        return bounds

    def place(self, xy, text, fill=PALE):
        x, y = xy
        size = 13
        for dx, dy in ((8, -23), (8, 4), (-34, -23), (-34, 4), (12, -39)):
            box = self._available_at(x + dx, y + dy, text, size)
            if box is None:
                continue
            self.draw.rounded_rectangle(box, radius=3, fill=INK)
            self.draw.text((x + dx, y + dy), text, fill=fill, font=font(size))
            self.used.append(box)
            return True
        return False


def _field_mark(draw, labels, mapping, item):
    region = field(item)
    outline = [mapping.coords(p) for p in region.corners()]
    _outline(draw, outline, mapping.destination, MINT)
    origin = outline[0]
    u = mapping.coords(region.to_source(Vec(min(region.width, 29), 0)))
    v = mapping.coords(region.to_source(Vec(0, min(region.height, 29))))
    _line(draw, origin, u, mapping.destination, WARM, width=3)
    _line(draw, origin, v, mapping.destination, "#9aceea", width=3)
    labels.place(origin, "U/V", MINT)


def _waste_marks(draw, labels, mapping, items):
    for index, item in enumerate(items, start=1):
        shape = field(item["shape"])
        corners = [mapping.coords(p) for p in shape.corners()]
        _outline(draw, corners, mapping.destination, DEFECT_COLOR, dashed=True)
        center = mapping.coords(shape.center)
        if mapping.visible(center):
            labels.place(center, f"D{index}", DEFECT_COLOR)


def _manual_points(draw, labels, mapping, points, show_labels):
    for index, point in enumerate(points, start=1):
        xy = mapping.coords(point)
        if mapping.visible(xy):
            color = PALETTE[point["mineral"]]["color"]
            _style_mark(draw, xy, color, 4)
            if show_labels:
                labels.place(xy, f"P{index}", color)


def _grain_marks(draw, labels, mapping, grains):
    for index, item in enumerate(grains, start=1):
        outline = [mapping.coords(p) for p in item["vertices"]]
        color = PALETTE[item["mineral"]]["color"]
        _outline(draw, outline, mapping.destination, color, width=2)
        if "metrics" in item:
            center = mapping.coords(item["metrics"]["centroid"])
        else:
            center = tuple(sum(pair[d] for pair in outline) / len(outline) for d in (0, 1))
        labels.place(center, f"G{index}", color)


def _line_mark(draw, labels, mapping, specification, color, label, dashed):
    a, b = mapping.coords(specification["start"]), mapping.coords(specification["end"])
    _line(draw, a, b, mapping.destination, color, dashed=dashed, width=2)
    for xy in (a, b):
        if mapping.visible(xy):
            _style_mark(draw, xy, color, 3)
    if mapping.visible(a):
        labels.place(a, label, color)


def add_markups(surface, study, mapping, annotate=Annotate()):
    canvas = ImageDraw.Draw(surface)
    regions = study["state"]
    labels = Labels(canvas, mapping.destination)
    if annotate.field:
        _field_mark(canvas, labels, mapping, regions["field"])
    if annotate.defects:
        _waste_marks(canvas, labels, mapping, regions["defects"])
    if annotate.guide:
        for record in study["guide"]["positions"]:
            xy = mapping.coords(record)
            if mapping.visible(xy):
                _style_mark(canvas, xy, "#b4d9ba", 2, outlined=False)
    if annotate.points:
        _manual_points(canvas, labels, mapping, regions["points"], len(regions["points"]) < 45)
    if annotate.grains:
        _grain_marks(canvas, labels, mapping, regions["grains"])
    if annotate.line:
        _line_mark(canvas, labels, mapping, regions["transect"], WARM, "T", True)
        scale = regions["scale"]
        if scale:
            if scale.get("mode") == "anisotropic":
                for ruler in scale.get("rulers", []):
                    _line_mark(
                        canvas, labels, mapping, ruler["line"], PALE,
                        f"{ruler['length_um']:g} um", False,
                    )
            else:
                _line_mark(canvas, labels, mapping, scale["line"], PALE, f"{scale['length_um']:g} um", False)


def color_swatch(draw, xy, color, radius=4):
    _style_mark(draw, xy, color, radius, outlined=False)


def study_heading(draw, title, rect, study):
    fitted_text(draw, (rect.x, rect.y), title, rect.w, 31)
    identity = printable(study["specimen"]) or study["id"]
    revised = study["modified"][:10]
    label = "Synthetic practice" if study["synthetic"] else "Thin section observation"
    context = f"{label}  |  {identity}  |  r{study['revision']}  |  {revised}"
    fitted_text(draw, (rect.x, rect.y + 42), context, rect.w, 16, DIM)


def photo_panel(surface, image, rect, title, detail=None):
    draw = ImageDraw.Draw(surface)
    set_panel(draw, rect.bounds)
    fitted_text(draw, (rect.x + 13, rect.y + 10), title, rect.w - 24, 17)
    if detail:
        fitted_text(draw, (rect.x + 13, rect.bottom() - 25), detail, rect.w - 26, 13, DIM)
    destination = Rect(rect.x + 12, rect.y + 40, rect.w - 24, rect.h - (72 if detail else 53))
    return paste_image(surface, image, destination)


def field_plate(surface, image, study, rect, annotation, corrected=False):
    if corrected:
        geometry = field(study["state"]["field"])
        title = "Rectified observation field"
        detail = f"U {geometry.width:g} px  |  V {geometry.height:g} px  |  angle {geometry.angle:g} deg"
        actual = photo_panel(surface, image, rect, title, detail)
        coordinate_map = Mapping(geometry.width, geometry.height, actual, geometry)
    else:
        title = "Primary view with observation marks"
        detail = f"Source {study['width']} x {study['height']} px"
        actual = photo_panel(surface, image, rect, title, detail)
        coordinate_map = Mapping(study["width"], study["height"], actual)
    add_markups(surface, study, coordinate_map, annotation)
    return actual


def _summary_row(draw, column, y, label, value, fill=PALE):
    fitted_text(draw, (column.x, y), label, column.w * 0.62, 14, DIM)
    fitted_text(draw, (column.right(), y), value, column.w * 0.36, 15, fill, anchor="ra")


def legend_panel(surface, rect, study, summary):
    draw = ImageDraw.Draw(surface)
    set_panel(draw, rect.bounds)
    inner = rect.pad(15)
    fitted_text(draw, (inner.x, inner.y), "Field record", inner.w, 20)
    y = inner.y + 36
    record = study["state"]
    physical = record["scale"]
    if physical is None:
        scale_text = "unscaled"
    elif physical.get("mode") == "anisotropic":
        scale_text = (
            f"x {physical['um_per_px_x']:.3f}  y {physical['um_per_px_y']:.3f} um/px"
        )
    else:
        scale_text = f"{physical['um_per_px']:.3f} um/px"
    facts = [
        ("Usable field", f"{summary['usable_pixels']:,d} px2"),
        ("Excluded union", f"{summary['excluded_pixels']:,d} px2"),
        ("Manual point marks", str(study["tally"]["count"])),
        ("Traced grains", str(len(record["grains"]))),
        ("Pixel scale", scale_text),
    ]
    for label, value in facts:
        _summary_row(draw, inner, y, label, value)
        y += 23
    draw.line((inner.x, y + 4, inner.right(), y + 4), fill=BORDER)
    y += 16
    fitted_text(draw, (inner.x, y), "MARK KEY", inner.w, 13, MINT)
    y += 25
    for i, item in enumerate(record["defects"][:4], start=1):
        _summary_row(draw, inner, y, f"D{i}  {DEFECT_NAMES[item['kind']]}", "discard", DEFECT_COLOR)
        y += 19
    remaining = len(record["defects"]) - 4
    if remaining > 0:
        fitted_text(draw, (inner.x, y), f"+ {remaining} waste region(s) in the observation package", inner.w, 13, DIM)
        y += 20
    fitted_text(draw, (inner.x, y + 12), "T  transect   |   G  grain outline   |   P  point mark", inner.w, 13, WARM)


def contact_sheet(items, study, render, columns=3):
    width = 1440
    gap = 14
    margin = 25
    tile_width = (width - 2 * margin - (columns - 1) * gap) // columns
    tile_height = 320
    rows = max(1, math.ceil(len(items) / columns))
    height = 102 + rows * (tile_height + gap)
    surface = Image.new("RGB", (width, height), INK)
    canvas = ImageDraw.Draw(surface)
    study_heading(canvas, "Polarization contact sheet", Rect(margin, 20, width - margin * 2, 68), study)
    for index, item in enumerate(items):
        row, column = divmod(index, columns)
        rect = Rect(margin + column * (tile_width + gap), 96 + row * (tile_height + gap), tile_width, tile_height)
        mode = item["kind"]
        if mode == "primary":
            label = "Primary source"
        elif mode == "crosscheck":
            label = "Reference source"
        else:
            label = f"Stage {item['angle']:g} degrees"
        dimensions = f"{item['width']} x {item['height']} px"
        img = render(item)
        photo_panel(surface, img, rect, label, dimensions)
    return surface
