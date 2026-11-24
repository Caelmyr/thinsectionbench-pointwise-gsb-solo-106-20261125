"""Download endpoints for charts, proof sheets and portable work archives."""
from __future__ import annotations

import io

from flask import jsonify, request, send_file

from petro.analysis import CHANNELS
from petro.domain import InputError, one_of, whole

from . import exports
from .archives import verify_package
from .records import MAX_ARCHIVE_BYTES


def integer_arg(name, default, lower, upper):
    value = request.args.get(name)
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError as exc:
        raise InputError(f"{name} 应为数值") from exc
    return whole(number, name, lower, upper)


def toggle_arg(name, default):
    value = request.args.get(name)
    if value is None:
        return default
    if value not in {"0", "1"}:
        raise InputError(f"{name} 应为 0 或 1")
    return value == "1"


def downloadable(study, content, stem, ext, media):
    raw = io.BytesIO(content) if isinstance(content, bytes) else content
    filename = f"thin-section-{study['id'][:8]}-r{study['revision']}-{stem}.{ext}"
    response = send_file(raw, mimetype=media, as_attachment=True, download_name=filename, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    if not isinstance(content, bytes):
        response.call_on_close(raw.close)
    return response


def add_report_routes(app, bench, selected, public_study):
    @app.get("/api/studies/<study_id>/export/proof.png")
    def proof(study_id):
        document = selected(study_id, metrics=True)
        options = {
            "size": one_of(request.args.get("size", "wide"), exports.PLATE_SIZES, "校样尺寸"),
            "rectified": toggle_arg("rectified", False),
            "points": toggle_arg("points", True),
            "grains": toggle_arg("grains", True),
            "guide": toggle_arg("guide", False),
        }
        return downloadable(document, exports.proof_plate(bench, document, options), "proof", "png", "image/png")

    @app.get("/api/studies/<study_id>/export/contacts.png")
    def contacts(study_id):
        document = selected(study_id)
        columns = integer_arg("columns", 3, 2, 4)
        return downloadable(document, exports.contact_plate(bench, document, columns), "contacts", "png", "image/png")

    @app.get("/api/studies/<study_id>/export/chart/<chart>.png")
    def chart(study_id, chart):
        document = selected(study_id, metrics=True)
        filename = one_of(chart, {"profile", "histogram", "sequence", "rose"}, "图表")
        return downloadable(document, exports.chart_image(bench, document, filename), filename, "png", "image/png")

    @app.get("/api/studies/<study_id>/export/<kind>.csv")
    def table(study_id, kind):
        document = selected(study_id, metrics=kind == "grains")
        channel = one_of(request.args.get("channel", "luminance"), CHANNELS, "采样通道")
        if kind == "points":
            content = exports.point_table(document)
        elif kind == "tally":
            content = exports.tally_table(document)
        elif kind == "grains":
            content = exports.grain_table(document)
        elif kind == "discarded":
            content = exports.defect_table(document)
        elif kind == "histogram":
            content = exports.histogram_table(bench, document, channel)
        elif kind == "profile":
            content = exports.profile_table(bench, document, {
                "channel":channel,
                "mask":toggle_arg("mask", True),
                "smoothing":integer_arg("smoothing", 0, 0, 30),
            })
        elif kind == "sequence":
            scope = one_of(request.args.get("scope", "field"), {"field", "grain"}, "序列范围")
            content = exports.sequence_table(bench, document, scope)
        elif kind == "components":
            content = exports.component_table(bench, document, {
                "channel":channel,
                "threshold":integer_arg("threshold", 88, 0, 255),
                "polarity":one_of(request.args.get("polarity","dark"), {"dark", "bright"}, "阈值方向"),
            })
        else:
            raise InputError("没有这张 CSV 表")
        return downloadable(document, content, kind, "csv", "text/csv; charset=utf-8")

    @app.get("/api/studies/<study_id>/export/study.zip")
    def package(study_id):
        document = selected(study_id, metrics=True)
        content = exports.full_package(bench, document, toggle_arg("plate", True))
        return downloadable(document, content, "observation", "zip", "application/zip")

    @app.post("/api/studies/observation")
    def import_observation():
        request.max_content_length = MAX_ARCHIVE_BYTES + 512 * 1024
        item = request.files.get("observation")
        if item is None:
            raise InputError("请选择完整观察包")
        record, assets = verify_package(item.stream)
        identifier = bench.store.import_observation(record, assets)
        return jsonify(public_study(identifier)), 201
