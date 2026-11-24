#!/usr/bin/env python3
"""Standalone verification for the four-arrangement point-plan workflow.

Run from the repository root with the project environment on sys.path:
    python3 tools/verify_point_plans.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SITE = "/opt/pointwise-env/site-packages"
if Path(SITE).is_dir():
    sys.path.append(SITE)

from petro.domain import Field, Vec, default_state  # noqa: E402
from petro.geometry import (  # noqa: E402
    plan_basis_signature, plan_is_current, plan_tally, quadrant_of, stage_plans,
)
from petro.synthetic import HEIGHT, WIDTH, build_practice  # noqa: E402
from thinbench.server import create_app  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def test_geometry_units():
    box = Field(Vec(0, 0), 100, 80, 0)
    check("quadrant q1 (right/top)", quadrant_of(box, Vec(80, 10)) == "q1")
    check("quadrant q2 (left/top)", quadrant_of(box, Vec(10, 10)) == "q2")
    check("quadrant q3 (left/bottom)", quadrant_of(box, Vec(10, 70)) == "q3")
    check("quadrant q4 (right/bottom)", quadrant_of(box, Vec(80, 70)) == "q4")
    check("center line falls right/lower (q4)", quadrant_of(box, Vec(50, 40)) == "q4")

    state = default_state((WIDTH, HEIGHT))
    state["field"] = box.json()
    state["grid"] = {"step": 20, "phase_u": "early", "phase_v": "early"}
    plans = stage_plans(state["field"], state["grid"], [], (WIDTH, HEIGHT))
    check("four arrangements", len(plans["arrangements"]) == 4)
    check("all complete without defects", plans["any_complete"])
    check("recommended exists", plans["recommended"] is not None)
    for item in plans["arrangements"]:
        blocks = item["blocks"]
        check(
            f"block sum equals total for {item['phase_u']}/{item['phase_v']}",
            sum(blocks.values()) == item["total"],
            f"{blocks} vs {item['total']}",
        )
        check(
            f"positions carry quadrant for {item['phase_u']}/{item['phase_v']}",
            all(p["quadrant"] in {"q1", "q2", "q3", "q4"} for p in item["positions"]),
        )


def test_defect_exclusion():
    box = Field(Vec(0, 0), 100, 100, 0)
    # Cover the entire top-left quadrant with a waste box.
    defect = {"id": "d1", "kind": "bubble", "name": "废",
              "shape": Field(Vec(0, 0), 50, 50, 0).json()}
    state = default_state((200, 200))
    state["field"] = box.json()
    state["grid"] = {"step": 20, "phase_u": "early", "phase_v": "early"}
    plans = stage_plans(state["field"], state["grid"], [defect], (200, 200))
    for item in plans["arrangements"]:
        check(
            f"q2 empty after defect for {item['phase_u']}/{item['phase_v']}",
            item["blocks"]["q2"] == 0 and "q2" in item["missing"],
            str(item["blocks"]),
        )
    check("no complete arrangement when a quadrant is fully blocked",
          not plans["any_complete"] and plans["recommended"] is None)


def test_http_workflow():
    data_dir = tempfile.mkdtemp(prefix="tb-verify-")
    app = create_app(data_dir)
    client = app.test_client()
    created = client.post("/api/studies/practice")
    check("practice created", created.status_code == 200, created.status_code)
    doc = created.get_json()
    sid = doc["id"]

    preview = doc["plan_preview"]
    check("document includes plan preview", len(preview["arrangements"]) == 4)
    check("practice preview has recommendation", preview["any_complete"],
          str([(a["phase_u"], a["phase_v"], a["total"], a["blocks"]) for a in preview["arrangements"]]))
    for item in preview["arrangements"]:
        # No plan slot may sit inside a defect or outside the source image.
        for p in item["positions"]:
            inside = 0 <= p["x"] < doc["width"] and 0 <= p["y"] < doc["height"]
            if not inside:
                check(f"slot inside image {p}", False)
                break
        else:
            continue
        break
    else:
        check("all preview slots inside source image", True)

    rec_u, rec_v = preview["recommended"]
    chosen = next(a for a in preview["arrangements"]
                  if a["phase_u"] == rec_u and a["phase_v"] == rec_v)

    # Adopting an incomplete arrangement must be refused.
    incomplete = next((a for a in preview["arrangements"] if not a["complete"]), None)
    if incomplete is not None:
        refused = client.post(f"/api/studies/{sid}/state", json={
            "revision": doc["revision"], "kind": "point-plan-adopt",
            "value": {"phase_u": incomplete["phase_u"], "phase_v": incomplete["phase_v"]},
        })
        check("incomplete arrangement refused", refused.status_code == 400, refused.status_code)

    adopted = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point-plan-adopt",
        "value": {"phase_u": rec_u, "phase_v": rec_v},
    })
    check("recommended arrangement adopted", adopted.status_code == 200, adopted.status_code)
    doc = adopted.get_json()
    plan = doc["state"]["point_plan"]
    check("plan persisted with slot count", plan is not None and
          len(plan["positions"]) == chosen["total"],
          f"{len(plan['positions']) if plan else 0} vs {chosen['total']}")
    check("plan marked current", doc["plan_current"] is True)
    check("plan tally pending equals slots",
          doc["plan_tally"]["pending"] == chosen["total"] and doc["plan_tally"]["filled"] == 0)
    block_sums = {b["key"]: b["total"] for b in doc["plan_tally"]["blocks"]}
    check("plan tally blocks match preview", block_sums == chosen["blocks"],
          f"{block_sums} vs {chosen['blocks']}")

    # Freehand points are preserved and stay separate.
    free = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point",
        "value": {"x": 12, "y": 12, "mineral": "mica", "note": "自由手点"},
    })
    check("freehand point added", free.status_code == 200, free.status_code)
    doc = free.get_json()
    check("freehand point has no plan_id",
          all("plan_id" not in p for p in doc["state"]["points"] if p["note"] == "自由手点"))
    check("plan tally ignores freehand point", doc["plan_tally"]["filled"] == 0)
    check("manual tally counts freehand point", doc["tally"]["count"] == 7)

    # Fill two plan slots with mineral classes.
    slots = plan["positions"]
    first, second = slots[0], slots[1]
    rev = doc["revision"]
    r1 = client.post(f"/api/studies/{sid}/state", json={
        "revision": rev, "kind": "point",
        "value": {"x": first["x"], "y": first["y"], "mineral": "quartz",
                  "note": "方案点", "plan_id": first["id"]},
    })
    check("first plan slot filled", r1.status_code == 200, r1.status_code)
    doc = r1.get_json()
    r2 = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point",
        "value": {"x": second["x"], "y": second["y"], "mineral": "undetermined",
                  "note": "", "plan_id": second["id"]},
    })
    check("second plan slot filled", r2.status_code == 200, r2.status_code)
    doc = r2.get_json()
    check("plan tally counts filled only", doc["plan_tally"]["filled"] == 2 and
          doc["plan_tally"]["pending"] == chosen["total"] - 2)
    check("plan tally classified excludes undetermined",
          doc["plan_tally"]["classified"] == 1 and doc["plan_tally"]["undetermined"] == 1)

    # A plan with fills cannot be silently replaced by another arrangement.
    other = next(a for a in preview["arrangements"]
                 if (a["phase_u"], a["phase_v"]) != tuple(preview["recommended"]) and a["complete"])
    guarded = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point-plan-adopt",
        "value": {"phase_u": other["phase_u"], "phase_v": other["phase_v"]},
    })
    check("re-adoption with fills refused", guarded.status_code == 400, guarded.status_code)
    doc = client.get(f"/api/studies/{sid}").get_json()

    # Duplicate fill of the same slot is rejected.
    dup = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point",
        "value": {"x": first["x"], "y": first["y"], "mineral": "mica", "plan_id": first["id"]},
    })
    check("duplicate slot fill refused", dup.status_code == 400, dup.status_code)

    # Editing a filled slot without restating plan_id keeps its plan membership.
    filled_record = next(p for p in doc["state"]["points"] if p.get("plan_id") == first["id"])
    edited = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point", "id": filled_record["id"],
        "value": {"x": filled_record["x"], "y": filled_record["y"],
                  "mineral": "feldspar", "note": "改判"},
    })
    check("plan membership kept on edit", edited.status_code == 200, edited.status_code)
    doc = edited.get_json()
    edited_record = next(p for p in doc["state"]["points"] if p["id"] == filled_record["id"])
    check("edited record still linked to slot",
          edited_record.get("plan_id") == first["id"] and edited_record["mineral"] == "feldspar")
    check("plan tally still counts edited slot once", doc["plan_tally"]["filled"] == 2)

    # Moving a freehand edit onto a plan slot via plan_id must still be unique.
    clash = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point",
        "value": {"x": first["x"], "y": first["y"], "mineral": "mica", "plan_id": first["id"]},
    })
    check("second fill of occupied slot refused", clash.status_code == 400, clash.status_code)

    # Unknown slot id is rejected.
    bad = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point",
        "value": {"x": first["x"], "y": first["y"], "mineral": "mica", "plan_id": "ppdeadbeef0000"},
    })
    check("unknown slot id refused", bad.status_code == 400, bad.status_code)

    # Editing the grid step invalidates the plan and removes its fills.
    changed = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "grid",
        "value": {"step": doc["state"]["grid"]["step"] + 5,
                  "phase_u": doc["state"]["grid"]["phase_u"],
                  "phase_v": doc["state"]["grid"]["phase_v"]},
    })
    check("grid edit accepted", changed.status_code == 200, changed.status_code)
    doc = changed.get_json()
    check("plan dropped after step change", doc["state"]["point_plan"] is None)
    check("plan fills removed after step change",
          all(p.get("plan_id") is None for p in doc["state"]["points"]))
    check("freehand point survives plan invalidation",
          any(p["note"] == "自由手点" for p in doc["state"]["points"]))

    # Re-adopt then invalidate through a defect edit.
    preview2 = doc["plan_preview"]
    rec2 = preview2["recommended"]
    adopted2 = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point-plan-adopt",
        "value": {"phase_u": rec2[0], "phase_v": rec2[1]},
    })
    check("second adoption works", adopted2.status_code == 200, adopted2.status_code)
    doc = adopted2.get_json()
    old_rev = doc["revision"]
    defect_edit = client.post(f"/api/studies/{sid}/state", json={
        "revision": old_rev, "kind": "defect",
        "value": {"kind": "blur", "name": "新增失焦区",
                  "shape": {"x": 100, "y": 60, "width": 40, "height": 30, "angle": 17}},
    })
    check("defect edit accepted", defect_edit.status_code == 200, defect_edit.status_code)
    doc = defect_edit.get_json()
    check("plan dropped after defect change", doc["state"]["point_plan"] is None)

    # Stale revision is still rejected.
    stale = client.post(f"/api/studies/{sid}/state", json={
        "revision": old_rev, "kind": "point-plan-clear", "value": None,
    })
    check("stale revision rejected", stale.status_code == 409, stale.status_code)

    # Signature helper consistency.
    sig = plan_basis_signature(doc["state"]["field"], doc["state"]["defects"],
                               doc["state"]["grid"]["step"])
    check("signature is hex sha256", len(sig) == 64 and all(c in "0123456789abcdef" for c in sig))
    check("plan_is_current False without plan",
          plan_is_current(None, doc["state"]["field"], doc["state"]["defects"],
                          doc["state"]["grid"]["step"]) is False)

    # Archive round trip keeps the plan.
    preview3 = doc["plan_preview"]
    rec3 = preview3["recommended"]
    adopted3 = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point-plan-adopt",
        "value": {"phase_u": rec3[0], "phase_v": rec3[1]},
    })
    doc = adopted3.get_json()
    slot = doc["state"]["point_plan"]["positions"][0]
    filled = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point",
        "value": {"x": slot["x"], "y": slot["y"], "mineral": "pore", "plan_id": slot["id"]},
    })
    check("slot fill before export", filled.status_code == 200, filled.status_code)
    package = client.get(f"/api/studies/{sid}/export/study.zip?revision={filled.get_json()['revision']}")
    check("archive exported", package.status_code == 200, package.status_code)
    imported = client.post(
        "/api/studies/observation",
        data={"observation": (io.BytesIO(package.data), "observation.zip")},
        content_type="multipart/form-data",
    )
    check("archive imported", imported.status_code == 201, imported.status_code)
    if imported.status_code == 201:
        new_doc = imported.get_json()
        check("plan survived round trip",
              new_doc["state"]["point_plan"] is not None and
              len(new_doc["state"]["point_plan"]["positions"]) ==
              len(filled.get_json()["state"]["point_plan"]["positions"]))
        check("plan fill survived round trip",
              new_doc["plan_tally"]["filled"] == 1)
        plan_csv = client.get(f"/api/studies/{new_doc['id']}/export/point-plan.csv")
        check("plan csv exported", plan_csv.status_code == 200 and b"slot_id" in plan_csv.data)

    # Clearing the plan removes plan points but keeps freehand ones.
    doc = client.get(f"/api/studies/{sid}").get_json()
    cleared = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point-plan-clear", "value": None,
    })
    check("plan cleared", cleared.status_code == 200, cleared.status_code)
    doc = cleared.get_json()
    check("no plan after clear", doc["state"]["point_plan"] is None and doc["plan_tally"] is None)
    check("freehand retained after clear",
          any(p["note"] == "自由手点" for p in doc["state"]["points"]))

    # An empty plan may be replaced by another complete arrangement.
    fresh_preview = doc["plan_preview"]
    complete_arrangements = [a for a in fresh_preview["arrangements"] if a["complete"]]
    check("at least two complete arrangements for replacement test",
          len(complete_arrangements) >= 2, len(complete_arrangements))
    first_arr = complete_arrangements[0]
    adopted_a = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point-plan-adopt",
        "value": {"phase_u": first_arr["phase_u"], "phase_v": first_arr["phase_v"]},
    })
    check("fresh adoption after clear", adopted_a.status_code == 200, adopted_a.status_code)
    doc = adopted_a.get_json()
    second_arr = complete_arrangements[1]
    replaced = client.post(f"/api/studies/{sid}/state", json={
        "revision": doc["revision"], "kind": "point-plan-adopt",
        "value": {"phase_u": second_arr["phase_u"], "phase_v": second_arr["phase_v"]},
    })
    check("empty plan can be replaced", replaced.status_code == 200, replaced.status_code)
    doc = replaced.get_json()
    check("replacement phases match",
          doc["state"]["point_plan"]["phase_u"] == second_arr["phase_u"] and
          doc["state"]["point_plan"]["phase_v"] == second_arr["phase_v"])


def test_tally_block_details():
    plan = {
        "positions": [
            {"id": "pp0000", "x": 1, "y": 1, "u": 1, "v": 1, "quadrant": "q1"},
            {"id": "pp0001", "x": 2, "y": 2, "u": 2, "v": 2, "quadrant": "q1"},
            {"id": "pp0002", "x": 3, "y": 3, "u": 3, "v": 3, "quadrant": "q2"},
        ]
    }
    points = [
        {"id": "a", "x": 1, "y": 1, "mineral": "quartz", "note": "", "plan_id": "pp0000"},
        {"id": "b", "x": 9, "y": 9, "mineral": "mica", "note": ""},
    ]
    tally = plan_tally(plan, points)
    check("tally slots", tally["slots"] == 3 and tally["filled"] == 1 and tally["pending"] == 2)
    q1 = next(b for b in tally["blocks"] if b["key"] == "q1")
    check("tally per-block fill", q1["total"] == 2 and q1["filled"] == 1 and q1["pending"] == 1)


def test_dense_grid_short_circuit():
    box = Field(Vec(0, 0), 4000, 4000, 0)
    state = default_state((4200, 4200))
    state["field"] = box.json()
    state["grid"] = {"step": 4, "phase_u": "early", "phase_v": "early"}
    plans = stage_plans(state["field"], state["grid"], [], (4200, 4200))
    check("dense grid flagged", plans["message"] != "")
    check("dense grid has no positions", all(a["total"] == 0 for a in plans["arrangements"]))
    check("dense grid cannot be recommended", plans["recommended"] is None)


if __name__ == "__main__":
    test_geometry_units()
    test_defect_exclusion()
    test_dense_grid_short_circuit()
    test_tally_block_details()
    test_http_workflow()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print("all point-plan checks passed")
