"""Flask shell and JSON endpoints consumed by the microscope workbench."""
from __future__ import annotations

import io
import os
import socket
import sqlite3
from pathlib import Path

from flask import Response, jsonify, render_template, request, send_file, url_for
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from image_lab import create_app as preparation_app
from petro.domain import (
    DEFECTS, ConflictError, InputError, MissingError, id_value, number,
    object_value, one_of, text_value, whole,
)
from petro.raster import MAX_IMAGE_BYTES

from .storage import _clean_metadata, StudyStore
from .workbench import ANALYSES, Bench


def read_json(optional=False):
    if optional and not request.get_data(cache=True):
        return {}
    if not request.is_json:
        raise InputError("此操作需要 JSON 请求")
    return object_value(request.get_json(), "请求")


def param_number(name, default, minimum, maximum, label):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        raw = float(raw)
    except ValueError as exc:
        raise InputError(f"{label}应为数值") from exc
    return number(raw, label, minimum, maximum)


def param_bool(name, default=True):
    raw = request.args.get(name)
    if raw is None:
        return default
    if raw not in {"1", "0"}:
        raise InputError(f"{name} 应为 0 或 1")
    return raw == "1"


def upload_image():
    item = request.files.get("image")
    if item is None:
        raise InputError("请选择原图文件")
    raw = item.stream.read(MAX_IMAGE_BYTES + 1)
    return item.filename or "", raw


def form_number(name, label):
    try:
        return float(request.form.get(name, ""))
    except ValueError as exc:
        raise InputError(f"{label}应为数值") from exc


def download_bytes(content, name, mimetype):
    response = send_file(
        io.BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=name,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(data_directory=None):
    app_root = Path(__file__).resolve().parent.parent
    data_root = Path(data_directory or os.environ.get("THINBENCH_DATA", app_root / ".thinbench-data"))
    app = preparation_app(data_root / "photo-preparation")
    app.config.update(JSON_SORT_KEYS=False)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    store = StudyStore(data_root)
    store.initialize()
    bench = Bench(store)
    app.extensions["thinbench"] = bench

    def public_asset(study_id, item):
        result = {
            key: item[key]
            for key in ("id", "name", "angle", "kind", "width", "height", "original_format", "byte_count", "created")
        }
        result["url"] = url_for("source_image", study_id=study_id, item_id=item["id"])
        return result

    def public_study(study_id):
        doc = bench.document(study_id)
        doc["assets"] = [public_asset(study_id, item) for item in doc["assets"]]
        doc["palette"] = bench.palette()
        doc["defect_types"] = DEFECTS
        doc["preview_url"] = url_for("preview_image", study_id=study_id)
        return doc

    def selected(study_id, metrics=False):
        study = store.study(study_id, metrics=metrics)
        expected = request.args.get("revision")
        if expected is not None:
            try:
                parsed = int(expected)
            except ValueError as exc:
                raise InputError("工作版本无效") from exc
            if parsed != study["revision"]:
                raise ConflictError("此读数来自旧版本，请刷新后继续")
        return study

    @app.before_request
    def limit_microscope_upload():
        if request.path.startswith("/api/studies"):
            request.max_content_length = MAX_IMAGE_BYTES + 512 * 1024

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.path.startswith("/api/") and response.content_type.startswith("application/json"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(InputError)
    def invalid_input(error):
        status = 400
        if isinstance(error, ConflictError):
            status = 409
        elif isinstance(error, MissingError):
            status = 404
        return jsonify(error=str(error), status=status), status

    @app.errorhandler(BadRequest)
    def bad_json(_error):
        return jsonify(error="请求内容无法解析", status=400), 400

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        limit = "22 MiB" if request.path.startswith("/api/studies") else "96 MiB"
        return jsonify(error=f"上传内容过大，当前入口接受的上限为 {limit}", status=413), 413

    @app.errorhandler(sqlite3.OperationalError)
    def busy(error):
        app.logger.warning("Catalog operation failed: %s", error)
        return jsonify(error="工作柜暂时无法访问，请稍后重试", status=503), 503

    @app.get("/")
    def home():
        return render_template("workbench.html", seed={"analyses": list(ANALYSES)})

    @app.get("/health")
    def health():
        return jsonify(status="ok" if store.healthy() else "unavailable", app="thin-section-bench")

    @app.get("/api/studies")
    def list_studies():
        return jsonify(studies=store.listing())

    @app.post("/api/studies/practice")
    def practice_study():
        return jsonify(public_study(store.ensure_practice()))

    @app.post("/api/studies")
    def create_study():
        label, content = upload_image()
        metadata = _clean_metadata(request.form)
        identifier = store.create(metadata, content, label)
        return jsonify(public_study(identifier)), 201

    @app.get("/api/studies/<study_id>")
    def open_study(study_id):
        return jsonify(public_study(study_id))

    @app.patch("/api/studies/<study_id>")
    def rename_study(study_id):
        store.rename(study_id, read_json())
        return jsonify(public_study(study_id))

    @app.delete("/api/studies/<study_id>")
    def remove_study(study_id):
        body = read_json()
        store.remove_study(study_id, body.get("revision"), body.get("title"))
        bench.purge(study_id)
        return "", 204

    @app.post("/api/studies/<study_id>/state")
    def change_state(study_id):
        store.update_state(study_id, read_json())
        return jsonify(public_study(study_id))

    @app.post("/api/studies/<study_id>/sources")
    def attach_source(study_id):
        _, content = upload_image()
        options = request.form.to_dict()
        if options.get("kind") == "rotation":
            options["angle"] = form_number("angle", "载物台角")
        store.attach(study_id, form_number("revision", "工作版本"), options, content)
        return jsonify(public_study(study_id)), 201

    @app.get("/api/studies/<study_id>/sources/<item_id>.png")
    def source_image(study_id, item_id):
        item, path = store.asset(study_id, item_id)
        if not path.is_file():
            raise MissingError("未找到图像文件")
        return send_file(path, mimetype="image/png", conditional=True, etag=item["sha256"], max_age=1800)

    @app.delete("/api/studies/<study_id>/sources/<item_id>")
    def remove_source(study_id, item_id):
        store.remove_asset(study_id, read_json().get("revision"), item_id)
        bench.purge(study_id)
        return jsonify(public_study(study_id))

    @app.get("/api/studies/<study_id>/preview.png")
    def preview_image(study_id):
        mode = one_of(
            request.args.get("mode", "primary"),
            {"primary", "reference", "mix", "difference", "crop"},
            "预览方式",
        )
        mixing = round(param_number("mix", 0.5, 0, 1, "混合比例"), 3)
        document = selected(study_id)
        data = bench.preview(document, mode, mixing)
        return send_file(io.BytesIO(data), mimetype="image/png", max_age=60)

    @app.post("/api/studies/<study_id>/analysis/<operation>")
    def analyze(study_id, operation):
        study = selected(study_id)
        return jsonify(bench.analyze(study, operation, read_json(optional=True)))

    @app.post("/api/studies/<study_id>/grains/<grain_id>/section/recommend")
    @app.post("/api/studies/<study_id>/grains/<grain_id>/recommend")
    @app.post("/api/studies/<study_id>/grains/<grain_id>/recommendation")
    def recommend_grain_section(study_id, grain_id):
        study = selected(study_id)
        body = read_json(optional=True)
        arguments = {**body, "grain_id": grain_id}
        return jsonify(bench.analyze(study, "grain-section", arguments))

    @app.get("/api/studies/<study_id>/grains/<grain_id>/section/recommend")
    @app.get("/api/studies/<study_id>/grains/<grain_id>/recommend")
    @app.get("/api/studies/<study_id>/grains/<grain_id>/recommendation")
    def recommend_grain_section_get(study_id, grain_id):
        study = selected(study_id)
        raw_index = request.args.get("component_index", request.args.get("index"))
        arguments = {"grain_id": grain_id}
        if raw_index is not None:
            try:
                arguments["component_index"] = int(raw_index)
            except ValueError as exc:
                raise InputError("残片编号应为整数") from exc
        return jsonify(bench.analyze(study, "grain-section", arguments))

    @app.post("/api/studies/<study_id>/sections/recommend")
    def recommend_section_collection(study_id):
        study = selected(study_id)
        body = read_json(optional=True)
        return jsonify(bench.analyze(study, "grain-section", body))

    @app.post("/api/studies/<study_id>/grains/<grain_id>/section/adopt")
    @app.post("/api/studies/<study_id>/grains/<grain_id>/adopt-section")
    @app.post("/api/studies/<study_id>/grains/<grain_id>/adopt")
    def adopt_grain_section(study_id, grain_id):
        body = read_json(optional=True)
        revision = body.get("revision", request.args.get("revision", type=int))
        payload = body.get("value", body.get("section", {"component_index": body.get("component_index")}))
        store.update_state(study_id, {
            "revision": revision, "kind": "section", "id": grain_id, "value": payload,
        })
        return jsonify(public_study(study_id))

    @app.put("/api/studies/<study_id>/grains/<grain_id>/section")
    @app.post("/api/studies/<study_id>/grains/<grain_id>/section/save")
    def put_grain_section(study_id, grain_id):
        body = read_json(optional=True)
        revision = body.get("revision", request.args.get("revision", type=int))
        store.update_state(study_id, {
            "revision": revision, "kind": "section", "id": grain_id,
            "value": body.get("section", body.get("value", {})),
        })
        return jsonify(public_study(study_id))

    @app.post("/api/studies/<study_id>/photo-preparation")
    def open_photo_preparation(study_id):
        study = selected(study_id)
        source = bench.primary_record(study)
        image = bench.pixels_for(study, source).astype("float32") / 255
        asset = app.extensions["assets"].put(image, source["name"])
        name = f"{study['title'][:70]} · 交偏光原图"
        project = app.extensions["store"].create(name)
        app.extensions["frames"].add(project["id"], project["revision"], [asset["id"]])
        return jsonify(url=url_for("index", project=project["id"])), 201

    @app.get("/api/studies/<study_id>/checkpoints")
    def checkpoints(study_id):
        return jsonify(checkpoints=store.checkpoint_list(study_id))

    @app.post("/api/studies/<study_id>/checkpoints")
    def checkpoint(study_id):
        identifier = store.make_checkpoint(study_id, read_json())
        return jsonify(id=identifier, checkpoints=store.checkpoint_list(study_id)), 201

    @app.post("/api/studies/<study_id>/checkpoints/restore")
    def restore(study_id):
        store.restore_checkpoint(study_id, read_json())
        return jsonify(public_study(study_id))

    @app.delete("/api/studies/<study_id>/checkpoints/<checkpoint_id>")
    def delete_checkpoint(study_id, checkpoint_id):
        store.remove_checkpoint(study_id, checkpoint_id)
        return jsonify(checkpoints=store.checkpoint_list(study_id))

    from .report_routes import add_report_routes
    add_report_routes(app, bench, selected, public_study)

    return app
