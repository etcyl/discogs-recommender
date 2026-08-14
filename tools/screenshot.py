#!/usr/bin/env python3
"""Capture screenshots of the running app for the README.

Start the app first (uvicorn app:app --port 8000), then:

  python tools/screenshot.py --out docs
  python tools/screenshot.py --out docs/before --width 420   # mobile widths
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

PAGES = [
    ("home", "/"),
    ("radio", "/radio"),
    ("recommendations", "/recommendations"),
    ("collection", "/collection"),
    ("search", "/search"),
    ("likes", "/radio/likes"),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--out", type=Path, default=BASE_DIR / "docs")
    p.add_argument("--width", type=int, default=1440)
    p.add_argument("--height", type=int, default=900)
    p.add_argument("--full-page", action="store_true")
    p.add_argument("--settle", type=int, default=2500,
                   help="ms to wait after load for async content")
    p.add_argument("--only", action="append", default=[],
                   help="repeatable: capture only these page names")
    args = p.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    pages = [(n, u) for n, u in PAGES if not args.only or n in args.only]

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,
            color_scheme="dark",
        )
        page = ctx.new_page()
        for name, path in pages:
            url = args.base + path
            try:
                # Not "networkidle" — the radio page holds an SSE connection
                # open for as long as it is generating, so the network never
                # goes idle and every capture would time out.
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                print(f"  {name}: navigation failed ({type(e).__name__}) — skipped")
                continue
            page.wait_for_timeout(args.settle)  # let async banners/JS settle
            dest = args.out / f"{name}.png"
            page.screenshot(path=str(dest), full_page=args.full_page)
            print(f"  {name:16s} -> {dest}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
