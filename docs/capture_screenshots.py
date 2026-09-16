"""Capture the dashboard screenshots in `docs/screenshots/`.

Run the dashboard first, then this:

    python -m streamlit run dashboard/app.py --server.port 8501
    python docs/capture_screenshots.py

Scripted rather than hand-captured for one reason: the numbers on the page
move. Bronze prunes, the fixture window slides, the producer runs again. A
screenshot taken by hand in August cannot be refreshed without redoing the
framing, so it silently ages while the README goes on citing it. This can be
re-run in twenty seconds after any pipeline run.

Needs `playwright` (in the `dev` extra) but NOT its bundled browsers:
`channel="chrome"` drives the Chrome already installed on the machine, which
avoids a ~120MB download for four PNGs.

Two settings that are decisions, not defaults:

* `color_scheme="light"`. The page follows the viewer's dark/light preference,
  so a capture has to pick one. Light, because these are embedded in a README
  that GitHub renders on white, and a dark screenshot on a white page reads as
  a mistake.
* `device_scale_factor=2`. Retina density, so the text is legible when the
  image is scaled down in a document or projected.

Headless Chrome's own `--screenshot` was tried first and is not sufficient:
`--virtual-time-budget` fast-forwards VIRTUAL time and does not wait on real
network, so it captures Streamlit's loading skeleton. Waiting for a number
that only exists after Snowflake answers is the whole trick.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://localhost:8501"
OUT = Path(__file__).parent / "screenshots"

# Each shot scrolls its heading to the top of the viewport and captures the
# viewport. Element screenshots were tried and are worse: Streamlit nests every
# block in anonymous containers, so an element's box is either the bare text or
# the entire page, never the panel a reader means.
#
# ONE viewport size for all five, and it is never changed after load. Resizing
# between shots makes Vega tear down and redraw every chart, and the first
# attempt captured five panels of empty grey placeholder because of it.
VIEWPORT = {"width": 1600, "height": 1150}

SHOTS = [
    ("01-overview", "Arbibet — cross-bookmaker market signals"),
    ("02-signals", "The signals themselves"),
    ("03-line-movement", "How the price got there"),
    ("04-deep-dives", "Deep dives"),
    ("05-slip-verdicts", "Does this slip make sense?"),
]

# The deep dive lives on its own page, reached by a link, so it needs its own
# navigation rather than a scroll. The event id is read off the first card on
# the main page instead of being hard-coded: the five popular fixtures are
# computed from the slip corpus and will change as slips are re-fetched.
# `None` means the top of the page: the AI brief sits above the first heading,
# and it is the thing a reader meets first.
DEEP_DIVE_SHOTS = [
    ("06-fixture-brief", None),
    ("07-fixture-market", "How the books priced it"),
    ("08-fixture-history", "What both sides had been doing"),
]

# Streamlit's own chrome -- the running indicator, the Deploy button, the
# hamburger -- is the development harness, not the dashboard. It says "Stop"
# mid-run, which in a still image looks like part of the product.
_HIDE_CHROME = """
header[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
/* The download/search/fullscreen strip that hovers over each dataframe */
[data-testid="stElementToolbar"],
/* Vega's export/source menu, which hovers over the top-right of every chart */
.vega-embed details,
.vega-embed .vega-actions { display: none !important; }
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(
            viewport=VIEWPORT,
            device_scale_factor=2,
            color_scheme="light",
        )
        page.goto(URL, wait_until="networkidle")

        # The tell that every query has returned. "Markets settled" is a label
        # Streamlit renders immediately; its VALUE only exists once Snowflake
        # has answered, which is what must be waited for.
        page.wait_for_selector("text=/5,9\\d\\d,\\d\\d\\d/", timeout=120_000)

        # And the tell that the charts have actually PAINTED. Vega mounts its
        # container long before it draws into it, so waiting on the element is
        # not enough -- wait for the canvases to carry pixels.
        page.wait_for_function(
            """() => {
                const c = [...document.querySelectorAll('canvas')];
                return c.length >= 3 && c.every(x => x.width > 0 && x.height > 0);
            }""",
            timeout=60_000,
        )
        page.add_style_tag(content=_HIDE_CHROME)
        page.wait_for_timeout(1500)

        # Open the first slip's evidence table. Collapsed is right for a reader
        # working down the page and wrong for a screenshot: the whole claim of
        # that panel is that the model's input sits beside its output, and a
        # closed expander shows only the output.
        expanders = page.get_by_text("legs — the numbers behind this")
        if expanders.count():
            expanders.first.click()
            page.wait_for_timeout(1500)

        def capture(name: str, heading: str | None) -> None:
            # scrollIntoView rather than Playwright's scroll_into_view_if_needed:
            # the latter stops as soon as the element is visible ANYWHERE in
            # the viewport, which puts a heading at the bottom edge with its
            # panel off-screen below. The panel is the point, so the heading
            # goes to the top.
            if heading is None:
                page.evaluate("window.scrollTo(0, 0)")
            else:
                page.evaluate(
                    """(text) => {
                        const el = [...document.querySelectorAll('h1,h2,h3')]
                            .find(e => e.innerText.includes(text));
                        if (el) el.scrollIntoView({block: 'start'});
                    }""",
                    heading,
                )
            page.wait_for_timeout(1200)
            path = OUT / f"{name}.png"
            page.screenshot(path=str(path))
            print(f"  {path.name}  ({path.stat().st_size // 1024} KB)")

        for name, heading in SHOTS:
            capture(name, heading)

        # Follow the first deep-dive link rather than constructing a URL, so
        # the shot is of whatever fixture the page is actually offering.
        link = page.locator("a[href*='/fixture?event_id=']").first
        if link.count():
            # The href is page-relative ("/fixture?event_id=..."), which
            # Playwright will not navigate to on its own.
            href = str(link.get_attribute("href"))
            page.goto(URL.rstrip("/") + href, wait_until="networkidle")
            page.wait_for_selector("text=/How the books priced it/", timeout=120_000)
            page.wait_for_function(
                """() => {
                    const c = [...document.querySelectorAll('canvas, .js-plotly-plot')];
                    return c.length > 0;
                }""",
                timeout=60_000,
            )
            page.add_style_tag(content=_HIDE_CHROME)
            page.wait_for_timeout(3000)
            for name, heading in DEEP_DIVE_SHOTS:
                capture(name, heading)

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
