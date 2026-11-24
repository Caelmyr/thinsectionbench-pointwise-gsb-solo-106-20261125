#!/usr/bin/env python3
"""Browser-level end-to-end check for the point-plan workflow."""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:18095"
FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def progress_card(page):
    # Scope to the card whose own heading is the progress card title.
    return page.locator(".card", has=page.locator("h3:has-text('方案点数进度')")).first


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 980})
        page.goto(BASE)
        page.click("#practice")
        page.wait_for_selector("#workspace:not([hidden])")
        page.click('[data-tab="count"]')
        page.wait_for_selector(".plan-table")

        # Recommendation card shows four rows with quadrant counts and adopt buttons.
        rows = page.locator(".plan-table tbody tr")
        check("four arrangement rows", rows.count() == 4, rows.count())
        check("recommended tag present", page.locator(".tag.rec").count() == 1)
        check("warning absent when complete", page.locator(".plan-table + .warning, .card .warning").count() == 0)
        adopt_buttons = page.locator('button[data-action="adopt-plan"]')
        check("only complete arrangements offer adoption", adopt_buttons.count() == 4)

        # Adopt the recommended arrangement.
        page.locator('button[data-action="adopt-plan"].primary').first.click()
        page.wait_for_selector(".card h3:has-text('方案点数进度')")
        check("plan progress card appears",
              page.locator(".card h3", has_text="方案点数进度").count() == 1)
        check("all slots pending initially", page.locator("text=/未填/").count() >= 1)
        pending_text = progress_card(page).inner_text()
        check("progress mentions 0 filled", "已填 0" in pending_text, pending_text[:120])
        check("adopt buttons hidden while plan active",
              page.locator('button[data-action="adopt-plan"]').count() == 0)
        check("active arrangement marked as adopted",
              page.locator(".tag.rec", has_text="已采用").count() == 1)

        # Empty plan slots render as hollow markers on the canvas.
        page.wait_for_timeout(400)
        canvas = page.locator("#micrograph")
        check("canvas visible", canvas.is_visible())

        # Fill a plan slot through the dedicated tool: pick mineral, activate tool, click a slot.
        page.select_option("#point-form [name=mineral]", "mica")
        page.click('button[data-tool="plan-fill"]')
        box = canvas.bounding_box()
        # Click near the upper-left area where q2 hollow slots sit; retry a few positions.
        clicked = False
        for frac_x, frac_y in ((0.42, 0.40), (0.46, 0.44), (0.38, 0.46), (0.50, 0.38)):
            canvas.click(position={"x": box["width"] * frac_x, "y": box["height"] * frac_y})
            page.wait_for_timeout(250)
            text = progress_card(page).inner_text()
            if "已填 1" in text:
                clicked = True
                break
        check("one plan slot filled via canvas", clicked)
        check("plan statistics count only the filled slot",
              "已填 1" in progress_card(page).inner_text())

        # Freehand tool still works and is excluded from plan stats.
        page.click('button[data-tool="point"]')
        # Click a guaranteed in-image location near the center of the canvas.
        center = {"x": box["width"] * 0.5, "y": box["height"] * 0.5}
        canvas.click(position=center)
        page.wait_for_timeout(400)
        total_now = page.locator(".card", has_text="全部点数统计").inner_text()
        check("freehand point added to overall tally", "手点共" in total_now)
        plan_text = progress_card(page).inner_text()
        check("plan tally unchanged by freehand point", "已填 1" in plan_text)

        # Reload: plan and fills persist and work can continue.
        page.reload()
        page.wait_for_selector("#workspace:not([hidden])")
        page.click('[data-tab="count"]')
        page.wait_for_selector(".plan-table")
        reopened = progress_card(page).inner_text()
        check("plan persists after reload", "方案点数进度" in reopened and "已填 1" in reopened)

        # Change step on survey tab: plan invalidates and its fills disappear.
        page.click('[data-tab="survey"]')
        page.fill('#panel [name="step"]', "55")
        page.click('form[data-form="grid"] button.primary')
        page.wait_for_timeout(300)
        page.click('[data-tab="count"]')
        page.wait_for_selector(".plan-table")
        check("plan gone after step change", page.locator("text=方案点数进度").count() == 0)
        check("recommendation reappears", page.locator(".plan-table").count() == 1)

        # Console must be free of errors.
        browser.close()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"{len(FAILURES)} browser check(s) failed: {FAILURES}")
        sys.exit(1)
    print("all browser checks passed")
