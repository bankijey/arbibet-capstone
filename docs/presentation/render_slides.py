"""Render the .pptx to per-slide PNGs for visual QA.

    python docs/presentation/render_slides.py [slide numbers...]

LibreOffice converts the deck to PDF; PyMuPDF rasterises the pages. The skill's
recipe uses `pdftoppm`, which is Poppler and does not ship on Windows -- and
LibreOffice does not bring it. PyMuPDF is a pip install and does the same job.

This is the check that geometry alone cannot make: python-pptx can prove no
shape sits outside the canvas, but only a render shows text overflowing its
own box, columns that do not line up, or a diagram gone illegible.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DECK = HERE / "arbibet-capstone.pptx"
OUT = HERE / "qa"
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")


def main(wanted: list[int]) -> int:
    if not SOFFICE.exists():
        print(f"LibreOffice not found at {SOFFICE}")
        return 1

    OUT.mkdir(exist_ok=True)
    for stale in OUT.glob("slide-*.png"):
        stale.unlink()

    # Convert a COPY, never the deck itself. Headless soffice keeps running
    # after --convert-to and holds a `.~lock.<name>#` on whatever it opened,
    # which made the next `node build_pptx.js` fail with EBUSY. Pointing it at
    # a throwaway keeps the canonical file free.
    working = OUT / f"_render{DECK.suffix}"
    shutil.copy2(DECK, working)

    # --outdir, not a cd: soffice resolves relative paths against its own
    # working directory and silently writes the PDF somewhere else.
    subprocess.run(
        [str(SOFFICE), "--headless", "--convert-to", "pdf", "--outdir", str(OUT), str(working)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    pdf = OUT / f"{working.stem}.pdf"
    if not pdf.exists():
        print("no PDF produced")
        return 1

    import pymupdf

    doc = pymupdf.open(pdf)
    pages = wanted or range(1, doc.page_count + 1)
    for n in pages:
        if not 1 <= n <= doc.page_count:
            continue
        # 150 dpi: enough to read 7.5pt body text, small enough to eyeball.
        pix = doc[n - 1].get_pixmap(dpi=150)
        path = OUT / f"slide-{n:02d}.png"
        pix.save(path)
        print(path)
    print(f"{doc.page_count} slides in the deck")
    doc.close()
    working.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main([int(a) for a in sys.argv[1:]]))
