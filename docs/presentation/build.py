"""Build deck.html from deck.template.html, embedding the screenshots.

The template references screenshots as {{IMG:<name>}} tokens; this inlines
them as base64 data URIs so the deck is one self-contained file that can be
opened from disk, projected, or published as an artifact. Re-run after
`docs/capture_screenshots.py` so the slides carry current numbers.
"""

import base64
import pathlib
import re

here = pathlib.Path(__file__).parent
shots = here.parent / "screenshots"
tpl = (here / "deck.template.html").read_text(encoding="utf-8")


def _inline(m: "re.Match[str]") -> str:
    png = (shots / f"{m.group(1)}.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(png).decode()


out = re.sub(r"\{\{IMG:([\w-]+)\}\}", _inline, tpl)
(here / "deck.html").write_text(out, encoding="utf-8")
print(f"deck.html {len(out)/1e6:.2f} MB, {out.count('<section class=' + chr(34) + 'slide')} slides")
