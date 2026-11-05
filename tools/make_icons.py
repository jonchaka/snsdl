"""Render the app icons from app/static/icons/icon.svg into the committed PNGs.

Run once after changing icon.svg (not run by the tests or in Docker):

    .venv/bin/python tools/make_icons.py

It uses Playwright's Chromium from the project's .venv (`make browser-test` installs it).
Outputs, all in app/static/icons/:
  icon-192.png, icon-512.png   the SVG as is, transparent outside the rounded corners ("any")
  icon-maskable-512.png        solid #4f46e5 full bleed, the mark inside the central 60 %
                               (20 % padding each side) so launcher masks never clip it
  apple-touch-icon.png         180 x 180, as "any" (iOS rounds the corners itself)
"""
import sys
from pathlib import Path

ICONS = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG = "#4f46e5"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright is not installed: run `make browser-test` (or `uv pip install playwright` "
             "and `.venv/bin/playwright install chromium`) first")


def _html(svg: str, size: int, maskable: bool) -> str:
    inner = round(size * 0.6) if maskable else size
    background = BG if maskable else "transparent"
    sized = svg.replace("<svg ", f'<svg width="{inner}" height="{inner}" style="display:block" ', 1)
    return (f'<!doctype html><html><body style="margin:0;width:{size}px;height:{size}px;'
            f'background:{background};display:flex;align-items:center;justify-content:center">'
            f'{sized}</body></html>')


def main() -> None:
    svg = (ICONS / "icon.svg").read_text()
    targets = [("icon-192.png", 192, False), ("icon-512.png", 512, False),
               ("icon-maskable-512.png", 512, True), ("apple-touch-icon.png", 180, False)]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, size, maskable in targets:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(_html(svg, size, maskable))
            page.screenshot(path=str(ICONS / name), omit_background=not maskable)
            page.close()
            print(f"wrote {ICONS / name} ({size}x{size})")
        browser.close()


if __name__ == "__main__":
    main()
