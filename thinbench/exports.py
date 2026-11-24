"""Exports for microscope sessions, annotated plates and reproducible readouts."""
from __future__ import annotations

import csv
import io
import json
import re

from PIL import Image, ImageDraw

from petro import charts, markups
from petro.domain import InputError
from petro.geometry import (
    field_summary, manual_tally, physical_segment, plan_is_current, plan_tally,
    rose_for_grains, stage_guide,
)
from petro.raster import encode_png

from .archives import create_archive

LABELS = markups.LABS
PLATE_SIZES = {
    "compact": (1280, 965),
    "wide": (1560, 1130),
}
CSV_SAFETY = re.compile(r"^[\s\u200b]*[=+\-@]")


def _csv_value(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.9g}"
    if isinstance(value, str) and CSV_SAFETY.match(value):
        return "'" + value
    if value is None:
        return ""
    return value


def csv_bytes(header, rows):
    stream = io.StringIO(newline="")
    outgoing = csv.writer(stream, lineterminator="\r\n")
    outgoing.writerow(header)
    for row in rows:
        outgoing.writerow([_csv_value(value) for value in row])
    return stream.getvalue().encode("utf-8-sig")


def simple_json(record):
    return json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def point_table(study):
    header = (
        "index", "point_id", "source_x_px", "source_y_px", "class_id",
        "class", "readable", "plan_slot_id", "note",
    )
    return csv_bytes(header, (
        (index, item["id"], item["x"], item["y"], item["mineral"], LABELS[item["mineral"]],
         item["mineral"] != "undetermined", item.get("plan_id", ""), item["note"])
        for index, item in enumerate(study["state"]["points"], start=1)
    ))


def plan_slot_table(study):
    plan = study["state"].get("point_plan")
    if not plan:
        return csv_bytes(
            ("slot_id", "quadrant", "source_x_px", "source_y_px", "point_id",
             "class_id", "class", "note"),
            (),
        )
    filled = {item.get("plan_id"): item for item in study["state"]["points"] if item.get("plan_id")}
    rows = []
    for slot in plan["positions"]:
        record = filled.get(slot["id"])
        rows.append((
            slot["id"], slot["quadrant"], slot["x"], slot["y"],
            record["id"] if record else "",
            record["mineral"] if record else "",
            LABELS[record["mineral"]] if record else "",
            record["note"] if record else "",
        ))
    return csv_bytes(
        ("slot_id", "quadrant", "source_x_px", "source_y_px", "point_id",
         "class_id", "class", "note"),
        rows,
    )


def tally_table(study):
    header = ("class_id", "class", "count", "percent_of_recorded_points")
    tally = manual_tally(study["state"]["points"])
    return csv_bytes(header, (
        (item["key"], LABELS[item["key"]], item["count"], item["percent"]) for item in tally["minerals"]
    ))


def grain_table(study):
    headers = (
        "index", "grain_id", "label", "class_id", "centroid_x_px", "centroid_y_px",
        "area_px2", "perimeter_px", "feret_min_px", "feret_max_px", "long_axis_deg",
        "circularity", "convexity", "aspect_ratio", "area_um2",
        "perimeter_um", "feret_min_um", "feret_max_um",
    )

    def row(index, item):
        value = item["metrics"]
        properties = (
            "area_px2", "perimeter_px", "feret_min_px", "feret_max_px", "long_axis_deg",
            "circularity", "convexity", "aspect_ratio", "area_um2",
            "perimeter_um", "feret_min_um", "feret_max_um",
        )
        return (
            index, item["id"], item["name"], item["mineral"], value["centroid"]["x"], value["centroid"]["y"],
            *(value.get(key) for key in properties),
        )

    return csv_bytes(headers, (row(index, item) for index, item in enumerate(study["state"]["grains"], start=1)))


def defect_table(study):
    headers = (
        "index", "region_id", "reason", "description", "origin_x_px", "origin_y_px",
        "along_u_px", "along_v_px", "clockwise_deg", "included_pixels",
    )
    report = field_summary(study["state"], (study["width"], study["height"]))
    counts = {item["id"]: item["visible_pixels"] for item in report["defects"]}
    return csv_bytes(headers, (
        (index, item["id"], item["kind"], item["name"], item["shape"]["x"], item["shape"]["y"],
         item["shape"]["width"], item["shape"]["height"], item["shape"]["angle"], counts[item["id"]])
        for index, item in enumerate(study["state"]["defects"], start=1)
    ))


def profile_table(bench, study, options):
    result, _ = bench.run(study, "profile", options)
    headers = ("source_x_px", "source_y_px", "distance_px", "distance_um", "intensity", "masked")
    return csv_bytes(headers, (
        (item["x"], item["y"], item["distance_px"], item["distance_um"], item["value"], item["masked"])
        for item in result["samples"]
    ))


def _sequence_data(bench, study, scope="field"):
    return bench.run(study, "sequence", {"scope": scope})[0]


def sequence_table(bench, study, scope):
    report = _sequence_data(bench, study, scope)
    keys = ("min", "p05", "median", "mean", "p95", "max", "stddev")
    headings = ("frame", "name", "stage_deg", *keys, "clipped_dark", "clipped_light", "samples")
    return csv_bytes(headings, (
        (item["id"], item["name"], item["angle"], *(item["samples"][key] for key in keys),
         item["clipped_dark"], item["clipped_light"], report["pixels_per_frame"])
        for item in report["values"]
    ))


def component_table(bench, study, arguments):
    data, _ = bench.run(study, "components", arguments)
    return csv_bytes(
        ("label", "area_px2", "area_um2", "perimeter_px", "centroid_x", "centroid_y", "width", "height", "on_edge"),
        (
            (item["label"], item["area_px2"], item["area_um2"], item["perimeter_px"],
             item["centroid"]["x"], item["centroid"]["y"], item["bbox"]["width"], item["bbox"]["height"],
             item["on_edge"])
            for item in data["components"]
        ),
    )


def histogram_table(bench, study, channel):
    result, _ = bench.run(study, "histogram", {"channel":channel})
    return csv_bytes(
        ("channel", "from_inclusive", "to_exclusive", "pixels"),
        ((channel, item["from"], item["to"], item["count"]) for item in result["histogram"]),
    )


def with_derived(study):
    state = study["state"]
    dimensions = (study["width"], study["height"])
    plan = state.get("point_plan")
    return {
        **study,
        "tally": manual_tally(state["points"]),
        "guide": stage_guide(state["field"], state["grid"], dimensions, state["defects"]),
        "plan_tally": plan_tally(plan, state["points"]),
        "plan_current": plan_is_current(plan, state["field"], state["defects"], state["grid"]["step"]),
        "ruler": physical_segment(state["transect"], state["scale"]),
        "rose": rose_for_grains(state["grains"], 12),
    }


def proof_plate(bench, source, options):
    study = with_derived(source)
    width, height = PLATE_SIZES[options["size"]]
    margin = 24
    gap = 14
    surface = Image.new("RGB", (width, height), markups.INK)
    canvas = ImageDraw.Draw(surface)
    title = markups.printable(study["title"]) or "Thin section proof"
    markups.study_heading(canvas, title, markups.Rect(margin, 22, width - margin * 2, 61), study)
    top = 96
    upper_height = round(height * 0.51)
    left_width = round(width * 0.67) - margin
    right_width = width - 2 * margin - gap - left_width
    right_start = margin + gap + left_width
    image_box = markups.Rect(margin, top, left_width, upper_height)
    legend_box = markups.Rect(right_start, top, right_width, upper_height)
    mode = "crop" if options["rectified"] else "primary"
    original = bench.view_pixels(study, mode)
    photo = original if isinstance(original, Image.Image) else Image.fromarray(original)
    toggles = markups.Annotate(
        field=not options["rectified"], defects=True, points=options["points"],
        grains=options["grains"], guide=options["guide"], line=True,
    )
    markups.field_plate(surface, photo, study, image_box, toggles, options["rectified"])
    masks = field_summary(study["state"], (study["width"], study["height"]))
    markups.legend_panel(surface, legend_box, study, masks)
    lower = top + upper_height + gap
    lower_height = height - lower - margin
    lower_width = (width - margin * 2 - 2 * gap) // 3
    cells = [
        markups.Rect(margin + (lower_width + gap) * column, lower, lower_width, lower_height)
        for column in range(3)
    ]
    try:
        data, _ = bench.run(study, "histogram", {"bins":32})
    except InputError:
        charts.result_notice(surface, cells[0], "Field histogram", "No usable image pixels")
    else:
        charts.histogram_chart(surface, cells[0], data)
    profile, _ = bench.run(study, "profile", {"spacing":2})
    charts.profile_chart(surface, cells[1], profile)
    charts.tally_chart(surface, cells[2], study["tally"], LABELS)
    return encode_png(surface)


def contact_plate(bench, study, columns):
    ordered = sorted(
        study["assets"],
        key=lambda item: ({"primary":0, "crosscheck":1, "rotation":2}[item["kind"]], item["angle"] or 0),
    )
    return encode_png(markups.contact_sheet(
        ordered, study, lambda item: Image.fromarray(bench.pixels_for(study, item)), columns,
    ))


def chart_image(bench, source, kind):
    if kind == "profile":
        report, _ = bench.run(source, "profile", {})
        return encode_png(charts.full_chart(1000, 430, charts.profile_chart, report))
    if kind == "histogram":
        report, _ = bench.run(source, "histogram", {"bins":48})
        return encode_png(charts.full_chart(970, 420, charts.histogram_chart, report))
    if kind == "sequence":
        return encode_png(charts.full_chart(970, 420, charts.sequence_chart, _sequence_data(bench, source)))
    if kind == "rose":
        saved = rose_for_grains(source["state"]["grains"], 12)
        return encode_png(charts.full_chart(680, 520, charts.rose_chart, saved["bins"], "area",
                                           "Traced grain orientation", saved["strength"]))
    raise InputError("没有这个图表")


def all_readouts(bench, study):
    entry = with_derived(study)
    yield "readouts/points.csv", point_table(entry)
    yield "readouts/tally.csv", tally_table(entry)
    if entry["state"].get("point_plan"):
        yield "readouts/point-plan.csv", plan_slot_table(entry)
    yield "readouts/grains.csv", grain_table(entry)
    yield "readouts/discarded.csv", defect_table(entry)
    yield "readouts/profile.csv", profile_table(bench, study, {})
    try:
        histogram = histogram_table(bench, study, "luminance")
    except InputError:
        histogram = b"channel,from_inclusive,to_exclusive,pixels\r\n"
    yield "readouts/histogram.csv", histogram
    yield "readouts/field.json", simple_json({
        "ruler":entry["ruler"],
        "tally":entry["tally"],
        "plan_tally":entry["plan_tally"],
        "plan_current":entry["plan_current"],
        "rose":entry["rose"],
        "field":field_summary(entry["state"], (entry["width"], entry["height"])),
    })
    if any(item["kind"] == "rotation" for item in entry["assets"]):
        try:
            sequence = sequence_table(bench, study, "field")
        except InputError:
            pass
        else:
            yield "readouts/sequence.csv", sequence


def full_package(bench, study, include_plate):
    def extras():
        yield from all_readouts(bench, study)
        if include_plate:
            yield "plates/overview.png", proof_plate(
                bench, study, {"size":"compact", "rectified":False, "points":True,
                               "grains":True, "guide":False},
            )

    return create_archive(study, bench.store.asset_path, extras)
