# Deck palette, for colouring the Excalidraw diagrams

Excalidraw's Mermaid importer drops `classDef`, so colour is applied by hand
after import: select the nodes of a category, then set **Stroke** and
**Background** from the hex below (Excalidraw's colour picker takes hex).

The deck renders dark, so the dark-canvas column is the match. The light column
is there if you export onto white for print.

| Category | Nodes | Stroke | Bg (dark canvas) | Bg (white canvas) |
|---|---|---|---|---|
| **Store / table** | bronze, cold, fixtures, em, ae, d, sig, tick, hist, slipraw, aiout, gold | `#F2B33D` | `#1E2F28` | `#FBEFD6` |
| **Process / code** | pool, rate, dedup, two, silver, idx, prod, arb, ev, watch, dims, flat, settle, ticks, slipin, stg, ai, st | `#5DBE8A` | `#16241F` | `#E4F3EA` |
| **Queue / topic** | topic | `#F2B33D` | `#3A2E12` | `#FDF6E7` |
| **External / reference** | books, apif, xwalk, msport | `#E0553F` | `#2A1F12` | `#FBE4E0` |
| **Cross-cutting** | af, ci | `#7FB3E6` | `#16241F` | `#E8F1FA` |

Text on dark: `#EEF3EE`. Subgraph frame stroke: `#2A3D34`. Canvas: `#0E1A16`.

Use **Sloppiness: architect** and **Edges: sharp** — the hand-drawn default
fights the precision the numbers on these slides claim.

Export at 2x, transparent background OFF, then drop the PNGs in as
`pipeline-sources.png` and `pipeline-capstone.png` and re-run `build.py` and
`build_pptx.js`. Nothing else needs to change.
