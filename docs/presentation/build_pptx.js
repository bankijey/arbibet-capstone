// Usage: npm install pptxgenjs && node build_pptx.js   (regenerates arbibet-capstone.pptx)
// Build the Arbibet capstone deck as a .pptx tuned for Google Slides import.
//
// Google Slides compatibility choices, deliberately:
//   * LAYOUT_16x9 (10 x 5.625 in) -- Slides' own default canvas, so nothing rescales.
//   * Fonts are Google-native: Barlow Condensed (titles), Arial (body), Courier New (mono).
//     Slides renders Barlow Condensed itself; anything else falls back to Arial cleanly.
//   * Images are PNG only (no SVG/EMF). No gradients, shadows, transparency, transitions,
//     animations or grouped shapes -- every one of those is where Slides import diverges.
//   * Speaker notes via addNotes(); tables via addTable(); hyperlinks as text runs.
//   * Text is sized conservatively: Slides does not honour PowerPoint autofit.
const pptxgen = require("pptxgenjs");

const OUT = "C:/Users/Dumebi/Arbibet/arbibet-capstone/docs/presentation/arbibet-capstone.pptx";
const PRES = "C:/Users/Dumebi/Arbibet/arbibet-capstone/docs/presentation/";
const SHOTS = "C:/Users/Dumebi/Arbibet/arbibet-capstone/docs/screenshots/";

// Floodlit pitch + odds board. One accent, two semantic colours.
const C = { bg: "0E1A16", surf: "16241F", surf2: "1E2F28", line: "2A3D34", ink: "EEF3EE",
            muted: "9DB0A6", faint: "6C8077", acc: "F2B33D", wrong: "E0553F", loud: "5DBE8A", info: "7FB3E6" };
const F = { d: "Barlow Condensed", b: "Arial", m: "Courier New" };

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";
pres.title = "Arbibet";
pres.author = "Arbibet capstone";

const TOTAL = 29;
let n = 0;

// ---------------------------------------------------------------- helpers
function slide(act, notes) {
  const s = pres.addSlide();
  s.background = { color: C.bg };
  n += 1;
  s.addText(act.toUpperCase(), { x: 0.5, y: 5.22, w: 5, h: 0.25, fontFace: F.m, fontSize: 8, color: C.faint, charSpacing: 2, margin: 0, isTextBox: true });
  s.addText(`${n} / ${TOTAL}`, { x: 8.5, y: 5.22, w: 1, h: 0.25, align: "right", fontFace: F.m, fontSize: 8, color: C.faint, margin: 0, isTextBox: true });
  if (notes) s.addNotes(notes);
  return s;
}
function head(s, eyebrow, title, size) {
  s.addText(eyebrow.toUpperCase(), { x: 0.5, y: 0.36, w: 9, h: 0.25, fontFace: F.m, fontSize: 9, color: C.acc, charSpacing: 2, margin: 0, isTextBox: true });
  s.addText(title.toUpperCase(), { x: 0.5, y: 0.6, w: 9, h: 0.8, fontFace: F.d, fontSize: size || 34, bold: true, color: C.ink, margin: 0, isTextBox: true, valign: "top" });
}
function card(s, x, y, w, h, fill) {
  s.addShape(pres.ShapeType.roundRect, { x, y, w, h, fill: { color: fill || C.surf }, line: { color: C.line, width: 0.75 }, rectRadius: 0.06 });
}
function h3(s, text, x, y, w) {
  s.addText(text.toUpperCase(), { x, y, w, h: 0.26, fontFace: F.d, fontSize: 14, bold: true, color: C.ink, charSpacing: 1, margin: 0, isTextBox: true });
}
// items: string | array of {text, options}. Bullet on the first run, break on the last.
function bullets(s, items, x, y, w, h, size, color) {
  const paras = [];
  items.forEach((it, i) => {
    const runs = (typeof it === "string" ? [{ text: it, options: {} }] : it.map(r => ({ text: r.text, options: Object.assign({}, r.options || {}) })));
    runs[0].options.bullet = true;
    runs[0].options.paraSpaceAfter = 4;
    runs[runs.length - 1].options.breakLine = i < items.length - 1;
    paras.push(...runs);
  });
  s.addText(paras, { x, y, w, h, fontFace: F.b, fontSize: size || 10.5, color: color || C.ink, valign: "top", margin: 0.02, isTextBox: true });
}
const A = (text) => ({ text, options: { bold: true, color: C.acc } });   // accent run
const I = (text) => ({ text, options: { italic: true } });
const T = (text) => ({ text, options: {} });
const M = (text) => ({ text, options: { fontFace: F.m, fontSize: 9.5, color: C.info } });

function stat(s, x, y, w, h, num, label, numSize) {
  card(s, x, y, w, h);
  s.addText(num, { x: x + 0.2, y: y + 0.12, w: w - 0.4, h: 0.85, fontFace: F.d, fontSize: numSize || 40, bold: true, color: C.acc, margin: 0, isTextBox: true, valign: "middle" });
  s.addText(label, { x: x + 0.2, y: y + 1.0, w: w - 0.4, h: h - 1.15, fontFace: F.b, fontSize: 10.5, color: C.muted, valign: "top", margin: 0, isTextBox: true });
}
function gotcha(s, x, y, w, h, kind, label, text) {
  const col = kind === "loud" ? C.loud : C.wrong;
  card(s, x, y, w, h, C.surf2);
  s.addShape(pres.ShapeType.ellipse, { x: x + 0.18, y: y + 0.2, w: 0.13, h: 0.13, fill: { color: col }, line: { color: col, width: 0 } });
  s.addText(label.toUpperCase(), { x: x + 0.4, y: y + 0.12, w: w - 0.55, h: 0.28, fontFace: F.m, fontSize: 8, color: col, charSpacing: 1.5, margin: 0, isTextBox: true });
  s.addText(text, { x: x + 0.18, y: y + 0.46, w: w - 0.36, h: h - 0.58, fontFace: F.b, fontSize: 9.5, color: C.ink, valign: "top", margin: 0, isTextBox: true });
}
function chips(s, items, x, y, w) {
  let cx = x, cy = y;
  items.forEach((t, i) => {
    const cw = Math.min(w, t.length * 0.062 + 0.28);
    if (cx + cw > x + w + 0.01) { cx = x; cy += 0.33; }
    const on = i === 0;
    s.addShape(pres.ShapeType.roundRect, { x: cx, y: cy, w: cw, h: 0.27, fill: { color: C.surf }, line: { color: on ? C.acc : C.line, width: 0.75 }, rectRadius: 0.04 });
    s.addText(t, { x: cx, y: cy, w: cw, h: 0.27, fontFace: F.m, fontSize: 7.5, color: on ? C.acc : C.ink, align: "center", valign: "middle", margin: 0, isTextBox: true });
    cx += cw + 0.08;
  });
}
function layer(s, o) {
  card(s, 0.5, 1.5, 5.3, 3.55);
  h3(s, "What it does", 0.7, 1.62, 4.9);
  bullets(s, o.what, 0.7, 1.95, 4.95, 3.0, 10.5);
  card(s, 6.05, 1.5, 3.45, 1.2);
  h3(s, "Tooling", 6.25, 1.6, 3.1);
  chips(s, o.tools, 6.25, 1.92, 3.1);
  gotcha(s, 6.05, 2.85, 3.45, 2.2, o.kind || "wrong", o.label || "Silently wrong", o.gotcha);
}
function sources(s, list) {
  const runs = [{ text: "Sources: ", options: { color: C.faint } }];
  list.forEach((it, i) => {
    runs.push({ text: it.t, options: { hyperlink: { url: it.u }, color: C.faint } });
    if (i < list.length - 1) runs.push({ text: "  ·  ", options: { color: C.faint } });
  });
  s.addText(runs, { x: 0.5, y: 4.8, w: 9, h: 0.38, fontFace: F.b, fontSize: 7.5, margin: 0, isTextBox: true, valign: "top" });
}
function placeholder(s, text, x, y, w, h) {
  s.addShape(pres.ShapeType.roundRect, { x, y, w, h, fill: { color: C.bg }, line: { color: C.acc, width: 1, dashType: "dash" }, rectRadius: 0.04 });
  s.addText(text, { x: x + 0.1, y, w: w - 0.2, h, fontFace: F.m, fontSize: 8.5, color: C.acc, valign: "middle", margin: 0, isTextBox: true });
}
function para(s, text, x, y, w, h, size, color) {
  s.addText(text, { x, y, w, h, fontFace: F.b, fontSize: size || 11, color: color || C.muted, valign: "top", margin: 0, isTextBox: true });
}

// ================================================================ ACT 1 · WHY
{
  const s = slide("Act 1 · Why · 1:00",
    "Open on the odds board top-right: two real prices from two real books that add up to a guaranteed 3.96% return on first-half corners. That is one of exactly four true surebets this platform found in 6.9 million payloads. Tell them the deck is about how we got to a number we can trust, and everything that tried to stop us. ~1 min.");
  s.addText([
    T("bet9ja   "), A("2.54"), T("\nsportybet   "), A("1.76"), T("\n1st half corners o/u 5.5\narbitrage "), A("1.0396"),
  ], { x: 6.3, y: 0.4, w: 3.2, h: 1.1, fontFace: F.m, fontSize: 9.5, color: C.faint, align: "right", margin: 0, isTextBox: true });
  s.addText("Arbibet — a cross-bookmaker data platform", { x: 0.5, y: 1.55, w: 8, h: 0.3, fontFace: F.m, fontSize: 11, color: C.muted, charSpacing: 1, margin: 0, isTextBox: true });
  s.addText("FIVE BOOKS.\nONE HONEST NUMBER.", { x: 0.5, y: 1.9, w: 9, h: 1.7, fontFace: F.d, fontSize: 60, bold: true, color: C.ink, margin: 0, isTextBox: true, valign: "top" });
  para(s, "From a Go odds collector to a Snowflake warehouse and a Streamlit dashboard — and what went silently wrong on the way.", 0.5, 3.65, 6.2, 0.7, 14, C.ink);
  placeholder(s, "[YOUR NAME]", 0.5, 4.5, 2.2, 0.36);
  placeholder(s, "[IRONHACK DE COHORT]", 2.85, 4.5, 2.6, 0.36);
  placeholder(s, "[DATE]", 5.6, 4.5, 1.4, 0.36);
}
{
  const s = slide("Act 1 · Why · 1:15",
    "Keep it brisk — three numbers, one sentence each. The point is not the size, it is the direction. Market-size estimates differ by a few billion between houses; say 'roughly' and move on. ~1:15.");
  head(s, "Why this, why now", "The world is betting");
  stat(s, 0.5, 1.55, 2.85, 2.0, "$125B", "global sports betting market, 2026 — up from $119B in 2025");
  stat(s, 3.55, 1.55, 2.85, 2.0, "44%", "of that market is Europe; North America is the fastest-growing region");
  stat(s, 6.6, 1.55, 2.85, 2.0, "$326B", "projected by 2035 — an 11% compound growth rate from 2026");
  para(s, "Legalisation, smartphones and digital payments are the drivers cited across every report — the same three things that make Nigeria the case study two slides on.", 0.5, 3.75, 9, 0.7, 11.5);
  sources(s, [
    { t: "The Business Research Company, Sports Betting Global Market Report 2026", u: "https://www.thebusinessresearchcompany.com/report/sports-betting-global-market-report" },
    { t: "Precedence Research, to 2035", u: "https://www.precedenceresearch.com/sports-betting-market" },
    { t: "Grand View Research", u: "https://www.grandviewresearch.com/industry-analysis/sports-betting-market-report" },
  ]);
}
{
  const s = slide("Act 1 · Why · 1:15",
    "The bridge slide. Prediction markets prove that a probability is a tradeable asset. Arbibet treats bookmaker prices the same way: five independent estimates of the same event, and the gaps between them are the signal. ~1:15.");
  head(s, "Why this, why now", "Odds became a market of their own");
  card(s, 0.5, 1.55, 5.5, 3.05);
  s.addText("$44.8B", { x: 0.7, y: 1.65, w: 5.1, h: 0.9, fontFace: F.d, fontSize: 44, bold: true, color: C.acc, margin: 0, isTextBox: true, valign: "middle" });
  para(s, "combined monthly volume on Kalshi + Polymarket, June 2026", 0.7, 2.55, 5.1, 0.4, 10.5);
  s.addText([T("That is more than "), A("triple"), T(" the ~$14B average "), I("monthly"), T(" handle of every legal US sportsbook in 2025. Nine months earlier it was under $5B a month.")],
    { x: 0.7, y: 3.0, w: 5.1, h: 1.4, fontFace: F.b, fontSize: 11.5, color: C.ink, valign: "top", margin: 0, isTextBox: true });
  card(s, 6.25, 1.55, 3.25, 3.05);
  h3(s, "What that means for us", 6.45, 1.67, 2.9);
  bullets(s, ["A price on an outcome is now a traded, public number.", "Disagreement between prices is the product — not a bug.", "Whoever reconciles prices fastest and most honestly wins."], 6.45, 2.0, 2.9, 2.5, 10.5);
  sources(s, [
    { t: "Pew Research Center, May 2026", u: "https://www.pewresearch.org/short-reads/2026/05/27/trading-volume-on-prediction-markets-has-soared-in-recent-months/" },
    { t: "TRM Labs, 2026", u: "https://www.trmlabs.com/resources/blog/how-prediction-markets-scaled-to-usd-21b-in-monthly-volume-in-2026" },
    { t: "KuCoin: combined 2025 volume > $44B", u: "https://www.kucoin.com/news/flash/kalshi-and-polymarket-combined-2025-trading-volume-surpasses-44-billion" },
  ]);
}
{
  const s = slide("Act 1 · Why · 1:30",
    "This is the 'why these five books' slide. Headline participation figures for Nigeria vary wildly by source — one widely-repeated figure exceeds the adult population — so this deck uses GeoPoll's survey rate and the revenue estimate only. If asked, say exactly that: the honest number is the surveyed one. ~1:30.");
  head(s, "Why this, why now", "Nigeria: a football-betting nation");
  stat(s, 0.5, 1.55, 2.85, 2.0, "54%", "of Nigerians surveyed placed a bet in the past 12 months (GeoPoll, July 2026)");
  stat(s, 3.55, 1.55, 2.85, 2.0, "~$590M", "sports-betting revenue in 2025, growing ~4.6% a year", 34);
  stat(s, 6.6, 1.55, 2.85, 2.0, "75–80%", "of handle is football; ~9 in 10 bets are placed on a phone", 34);
  para(s, "Five of the books in this platform — sportybet, msport, ilotbet, bet9ja, livescorebet — are the ones on those phones. The booking-slip culture (\"copy my code\") is the behaviour the AI layer studies.", 0.5, 3.75, 9, 0.8, 11.5);
  sources(s, [
    { t: "GeoPoll, Betting in Africa 2026", u: "https://www.geopoll.com/blog/betting-africa-2026/" },
    { t: "News Agency of Nigeria, 2026", u: "https://nannews.ng/article/sports-betting-in-nigeria-in-2026-market-growth-and-the-new-state-level-rules/" },
    { t: "iGaming Afrika, Nigeria market overview", u: "https://igamingafrika.com/nigeria-igaming-market-overview-1in-africa/" },
  ]);
}
{
  const s = slide("Act 1 · Why · 1:00",
    "One minute. Land the scale, then pivot: 'and every one of those wagers is a data point somebody had to reconcile.' ~1:00.");
  head(s, "Why this, why now", "World Cup 2026: the biggest betting event ever");
  stat(s, 0.5, 1.55, 2.85, 2.0, "$3.3B", "legal US handle on the tournament (Deutsche Bank); estimates ran to $4.3B — ~9× Qatar 2022");
  stat(s, 3.55, 1.55, 2.85, 2.0, "$50–60B", "wagered worldwide (Macquarie / H2 Gambling Capital), up from ~$35B in 2022", 34);
  stat(s, 6.6, 1.55, 2.85, 2.0, "10×", "\"ten Super Bowls\" — how FOX Sports summarised the tournament's total handle");
  para(s, "The platform was collecting through the tournament. The slips punters copied, and the prices five books put on them, are the data in this deck.", 0.5, 3.75, 9, 0.7, 11.5);
  sources(s, [
    { t: "CNBC, June 2026", u: "https://www.cnbc.com/2026/06/10/the-world-cup-will-likely-be-the-biggest-gambling-event-in-history.html" },
    { t: "Yogonet, July 2026", u: "https://www.yogonet.com/international/news/2026/07/21/125483-us-sportsbooks-call-2026-world-cup-the-biggest-betting-event-ever-after-record-final-handle" },
    { t: "FOX Sports recap", u: "https://www.foxsports.com/stories/soccer/2026-world-cup-betting-recap" },
    { t: "Casino.org", u: "https://www.casino.org/news/sports-betting-2026-world-cup-shatters-records/" },
  ]);
}
{
  const s = slide("Act 1 · Why · 1:30",
    "Frame the whole project as answering two questions, and promise the answers come in Act 3: the first is 'almost never, and here are the four times it did'; the second is 'usually not, and here is the arithmetic.' ~1:30.");
  head(s, "The business question", "People already pay for reconciled odds");
  card(s, 0.5, 1.55, 4.4, 3.1);
  h3(s, "OddsJam — the proof of demand", 0.7, 1.67, 4.0);
  bullets(s, [
    [T("Scans "), A("50+ sportsbooks"), T(" for positive-EV and arbitrage bets.")],
    [T("Subscriptions from "), A("$39"), T(" to "), A("$199"), T(" a month (top tiers to $999).")],
    "Founded 2020; still independent and growing.",
    [I("Sport is a data business already: every commentary line is a statistic — possession, xG, corners, form. The bet is just the price attached to it.")],
  ], 0.7, 2.0, 4.0, 2.6, 10.5);
  s.addShape(pres.ShapeType.roundRect, { x: 5.1, y: 1.55, w: 4.4, h: 3.1, fill: { color: C.surf }, line: { color: C.acc, width: 1.25 }, rectRadius: 0.06 });
  h3(s, "Arbibet's two questions", 5.3, 1.67, 4.0);
  bullets(s, [
    [A("Across five Nigerian and international books, do the prices ever disagree enough to matter?")],
    [A("When thousands of people copy a betting slip, does it actually make sense?")],
    "Both need the same thing: every book's prices on one taxonomy, joined to what actually happened.",
  ], 5.3, 2.0, 4.0, 2.6, 10.5);
  sources(s, [
    { t: "OddsJam subscription packages", u: "https://oddsjam.com/subscribe" },
    { t: "XCLSV, OddsJam review 2026", u: "https://xclsvmedia.com/oddsjam-review-2026-is-this-199-month-betting-tool-worth-it/" },
  ]);
}

// ================================================================ ACT 2 · WHAT
{
  const s = slide("Act 2 · What I built · 1:15",
    "Left to right, and none of this is the capstone -- it is the platform the capstone plugs into. A Go collector per book, writing bronze only when a response actually changes, which is why bronze doubles as a tick store later. The matcher decides which entries across books are the same fixture; API-Football is the record of what happened; silver knows how to settle a market. Point at ROLLING WINDOW on apifootball_events -- that is the biggest bug in the deck, and it is coming. ~1:15.");
  head(s, "The platform, part 1 of 2", "Where the data comes from");
  card(s, 0.5, 1.45, 9, 3.65);
  s.addImage({ path: PRES + "pipeline-sources.png", x: 0.6, y: 1.55, w: 8.8, h: 3.45, sizing: { type: "contain", w: 8.8, h: 3.45 } });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "Everything here is new. Two paths out of the same bronze: a live one through Redpanda to two detectors in their own consumer groups, and a batch one through Spark. The watcher is the freshness path added later -- same code as the consumers, MERGEd on the same key, so the two converge instead of duplicating. CORE is task-written and ANALYTICS is dbt's, so a full-refresh can never drop something a task paid an API call for. Then: 'each of the next twelve slides is one box on this chart.' ~1:30.");
  head(s, "The platform, part 2 of 2", "The capstone pipeline");
  card(s, 0.5, 1.45, 9, 3.65);
  s.addImage({ path: PRES + "pipeline-capstone.png", x: 0.6, y: 1.55, w: 8.8, h: 3.45, sizing: { type: "contain", w: 8.8, h: 3.45 } });
}
{
  const s = slide("Act 2 · What I built · 1:15",
    "Attribution matters here and the panel will respect it. Say plainly what you inherited and what you changed. The three contributions are concrete and yours. ~1:15.");
  head(s, "Layer 1 · Collection", "The Go collector");
  layer(s, {
    what: [
      [T("A pool of HTTP collectors hits "), A("20+ bookmaker APIs"), T(" on a per-plugin schedule — one plugin per book, so one broken book never stalls the rest.")],
      "Validates, deduplicates with a content-addressed cache, persists to Postgres.",
      [I("Inherited a v1 and re-platformed it."), T(" My changes: the "), A("MongoDB → Postgres migration"), T(", "), A("team-ID extraction"), T(", and the "), A("event-ID collision fix"), T(". The plugin architecture predates me.")],
    ],
    tools: ["Go", "Postgres", "Kafka-instrumented", "per-plugin rate limits"],
    gotcha: "Two different fixtures sharing one event id looked like one fixture with twice the prices. Nothing errored; the collision fix was found from the data, not a log.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "The single most important property of the whole platform is 'append on change, verbatim'. Everything downstream can be rebuilt from it — including the odds tick store in the capstone, which nobody planned but bronze already was. ~1:30.");
  head(s, "Layer 2 · Bronze", "Markets bronze: keep everything, verbatim");
  layer(s, {
    what: [
      [A("6.9 million payloads"), T(" from ~14 books, stored as the exact bytes each book returned — with payload_sha256, parser_version and run_id on every row.")],
      [A("Append on change:"), T(" a row is written only when a book's response differs from the last one. Bronze never updates.")],
      "Byte fidelity is the contract: a malformed body fails in the parser, where the failure has a reason, never on the way in.",
    ],
    tools: ["Postgres", "BYTEA", "medallion", "hash dedup"],
    gotcha: "\"Append on change\" means a book whose price has not moved contributes an hours-old leg to a \"snapshot\". An arbitrage across a fresh leg and a stale one is an artefact. The spread is now recorded on every signal — observed up to 30 hours.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:15",
    "The numbers are real and read straight from the parquet metadata. Fill in the intent: was cold = older than N days? Was the goal to free Postgres, or to query with DuckDB? What stopped it — disk, time, or the capstone deadline? ~1:15.");
  head(s, "Layer 2b · Cold storage", "The parquet lake attempt");
  card(s, 0.5, 1.55, 2.9, 3.35);
  s.addText("8.8M", { x: 0.7, y: 1.65, w: 2.5, h: 0.8, fontFace: F.d, fontSize: 40, bold: true, color: C.acc, margin: 0, isTextBox: true, valign: "middle" });
  para(s, "bronze rows exported to parquet", 0.7, 2.45, 2.5, 0.4, 10.5);
  s.addText("43 GB", { x: 0.7, y: 3.0, w: 2.5, h: 0.8, fontFace: F.d, fontSize: 40, bold: true, color: C.acc, margin: 0, isTextBox: true, valign: "middle" });
  para(s, "on local disk, D:\\arbibet-cold", 0.7, 3.8, 2.5, 0.4, 10.5);
  card(s, 3.6, 1.55, 5.9, 3.35);
  h3(s, "Two tiers, built with DuckDB", 3.8, 1.67, 5.5);
  s.addTable([
    [{ text: "TIER", options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7.5 } }, { text: "ROWS", options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7.5 } }, { text: "SIZE", options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7.5 } }, { text: "ROW GROUPS", options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7.5 } }],
    ["cold", { text: "3,853,417", options: { fontFace: F.m } }, { text: "16.5 GB", options: { fontFace: F.m } }, { text: "377", options: { fontFace: F.m } }],
    ["warm", { text: "4,936,610", options: { fontFace: F.m } }, { text: "26.4 GB", options: { fontFace: F.m } }, { text: "483", options: { fontFace: F.m } }],
  ], { x: 3.8, y: 2.0, w: 5.5, colW: [1.1, 1.6, 1.4, 1.4], fontFace: F.b, fontSize: 10, color: C.ink, fill: { color: C.surf }, border: { type: "solid", color: C.line, pt: 0.5 }, margin: 0.06 });
  para(s, "Same 12 columns as bronze — event, bookmaker, fire/ingest/write time, HTTP status, payload, sha256, parser version, run id — so it is a faithful mirror, not a summary.", 3.8, 3.15, 5.5, 0.6, 9.5);
  placeholder(s, "[YOUR INPUT] why two tiers, what worked, why it paused", 3.8, 3.95, 5.5, 0.55);
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "Two repos, one slide, because together they are 'identity and truth'. The rolling-window bug is the biggest one in the deck — it comes back in Act 3. Plant it here. ~1:30.");
  head(s, "Layer 3 · Identity & record", "Matcher + API-Football ingestor");
  layer(s, {
    what: [
      [A("Matcher:"), T(" Man Utd, Manchester Utd, Manchester United FC are one club to a human and three strings to a database. It decides which entries across books are the same fixture and persists a canonical match graph.")],
      [A("Ingestor:"), T(" the record of what actually happened — "), A("154k fixtures"), T(" as nested JSONB, with a two-phase fetch to live inside a per-minute rate limit and a daily quota, hash dedup, and idempotent resume from checkpoint.")],
      [T("Built from scratch as a "), I("disciplined rebuild of an earlier monolith"), T(".")],
    ],
    tools: ["Python", "Postgres · JSONB", "one-shot Docker", "checkpoint resume"],
    gotcha: "The matcher's API-Football projection is a rolling window. A fixture that resolved to team ids last week resolves to NULL today — and a plain MERGE wrote those NULLs over ids the warehouse already had. 3,226 fixtures blanked; nothing errored.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:15",
    "The deck's first green chip. Make the contrast explicit: a loud failure is a gift. The engine also insists side_or_line='home' means 'this team', not 'the home team' — which is why two teams' histories cannot always be pooled. ~1:15.");
  head(s, "Layer 4 · Settlement", "The silver settlement engine");
  layer(s, {
    what: [
      [A("4,874 lines"), T(" of resolvers that take a market, an outcome and a final score, and say won / lost / void — for every betradar market the books price.")],
      [T("Period-resolved scores (1st half, 2nd half, match), a per-team form grain, and one rule that cost a debate: "), A("\"full\" = regulation + extra time, never penalties"), T(".")],
      [T("Vendored into the capstone and run at scale: "), A("5.96M"), T(" settled team-market rows.")],
    ],
    tools: ["Python", "pure functions", "vendored"],
    kind: "loud", label: "Loudly wrong — the cheap kind",
    gotcha: "The engine wants ft/aet; the ingestor stores FT/AET. It refused all 87,600 rows as unknown_status. Annoying, obvious, fixed in minutes. Compare that to the slide before.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "Why a broker at all: the snapshot is expensive to build and TWO detectors need it, so a topic means building it once -- and a third detector later just subscribes, touching nothing upstream. Offsets are a rewind button after a bug fix. Why Redpanda: it speaks the Kafka API, so this is ordinary Kafka code that could point at managed Kafka tomorrow with one config change -- no lock-in; one binary, no ZooKeeper and no JVM, so one container instead of three; it fits in the one core and 1 GB of RAM the compose file gives it; the same broker in development as a real deployment; and free, when the whole project cost twenty cents. Say the counterpoint before they do: at this volume a queue is not strictly required, and the watcher on the next slide deliberately skips it. Kafka is here for the SHAPE -- fan-out -- not the throughput. If the panel asks why Kafka at all for a daily batch: the producer and consumers make one pass and exit, which is what lets them sit in a batch DAG — but the shape is the real-time shape, and the next slide is where it becomes real-time. ~1:30.");
  head(s, "Layer 5 · Streaming", "Kafka fan-out: one snapshot, two detectors");
  layer(s, {
    what: [
      [T("The producer reads each book's latest payload, parses it through the "), A("market crosswalk"), T(" (bet9ja and livescorebet speak their own dialects; three books are betradar-native) and publishes "), A("one fixture snapshot per message"), T(", keyed by event id.")],
      [T("Two consumers in "), A("separate consumer groups"), T(": arbitrage (1 / Σ(1 / best odds)) and positive-EV (price vs a published probability).")],
      [T("At-least-once: offsets commit after the write, and a "), A("MERGE on signal_key"), T(" absorbs any replay.")],
    ],
    tools: ["Redpanda", "confluent-kafka", "pandas crosswalk", "MERGE"],
    gotcha: "Measure a parser fix against messages produced before the fix and you measure the old parser. Three investigations were misled in one session. Rule: change parser → delete topic → delete groups → re-produce → then measure.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "This is how the legacy system worked and it was rebuilt on request in an afternoon because every piece already existed as a tested function. Verified live: new signals landed within one poll. ~1:30.");
  head(s, "Layer 5b · Freshness", "The watcher: recompute the moment prices land");
  layer(s, {
    what: [
      [T("Polls bronze every "), A("20 s"), T(" for the newest write per upcoming fixture; any fixture whose prices moved is re-run through the same snapshot → arbitrage/EV code the consumers use, and MERGEd on the same key — so the two paths converge.")],
      [A("Pre-kickoff only."), T(" A bet must still be placeable, and an in-play match is a payload storm.")],
      [A("Snowflake is touched only to write."), T(" No opportunity, no statement, no credits.")],
    ],
    tools: ["Python loop", "Postgres · indexed poll", "Snowflake MERGE"],
    label: "Nearly wrong, caught in the log",
    gotcha: "First run: a 3-day window = 1,368 fixtures, and the poll became a 40-second seq scan of the 6.9M-row table. A 6-hour window and an indexed key: under a second. Then it primed a baseline instead of recomputing 1,300 fixtures the DAG already covered.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "The Spark code in one breath. flatten.py is the ONLY real Spark job. read_payloads does a JDBC partitioned read -- eight partitions keyed on the bronze id, so eight workers pull in parallel instead of one connection dragging 505 MB. parse applies from_json with a PARTIAL schema: declare only the fields you want and Spark discards the rest, which is how nested JSONB becomes columns with no manual traversal. latest_per_fixture is a window dedup; team_statistics is an explode-then-pivot rather than a UDF, because the stat labels are known and a pivot stays in the JVM; team_rows makes the two team-rows per fixture and self-joins to attach the OPPONENT's xG. The DataFrame is cached because three branches read it -- both sides and the pivot -- and without that the 505 MB parse runs three times. settle.py is NOT Spark and its docstring says so: the input is already-flattened fact_team_match in Snowflake, and the work is a nested loop in Python over ~225 (market, side) combinations per fixture calling the vendored settle() and team_perspective(). If asked why it lives in spark/: naming it honestly matters more than a tidy folder. Windows gotcha for the panel: spark.jars.packages needs a Hadoop temp dir Windows cannot provide without winutils.exe, so Spark runs inside the Airflow image. Then --env-file .env passed the HOST's JAVA_HOME into the Linux container and the JVM never started. Both in the troubleshooting slide. ~1:30.");
  head(s, "Layer 6 · Batch", "Flatten with Spark, settle in Python");
  layer(s, {
    what: [
      [A("flatten:"), T(" 154k nested-JSONB fixtures → "), A("298k"), T(" wide team-match rows: period scores, xG, shots, corners, possession, passes. JDBC partitioned reads, from_json with a partial schema, pivot, self-join.")],
      [A("settle:"), T(" every team-match row × every market the books price, through the vendored engine → "), A("5.96M"), T(" results. "), I("Not a Spark job"), T(" -- the input is already flat, so it is a Python loop over Snowflake.")],
      "Incremental in the DAG (FLATTEN_SINCE_DAYS=3); the full backfill is an 80-minute, 505 MB scan.",
    ],
    tools: ["PySpark 4.2", "JDK 17", "settle: plain Python", "runs in the Airflow image"],
    kind: "loud", label: "Loudly wrong — but expensively",
    gotcha: "One fixture in 153,386 had no score. A NOT NULL failed the write — after 80 minutes. Two such runs were lost. Rule now: prove a long job on a 3,000-row bound before running it whole.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "The timezone one is a good teaching moment because the data was never wrong — only the rendering. Fix was three lines: pin the session to Europe/Berlin in the connector and dbt, and cast to TIMESTAMP_LTZ where it is displayed. ~1:30.");
  head(s, "Layer 7 · Warehouse", "Snowflake: a medallion with two owners");
  layer(s, {
    what: [
      [A("CORE"), T(" is task-written (consumers, Spark, the LLM tasks); "), A("ANALYTICS"), T(" is dbt-written. A dbt full-refresh can never drop something a task paid an API call for.")],
      [T("Snowflake enforces "), A("NOT NULL and nothing else"), T(" — PRIMARY KEY is documentation. So every write is a MERGE on a natural key, and re-running anything converges.")],
      "Loads stage through write_pandas + one MERGE after a row-at-a-time loader died at 446 of 4,527 rows and left the table silently partial.",
    ],
    tools: ["Snowflake", "MERGE", "write_pandas", "TIMESTAMP_TZ"],
    gotcha: "Session TIMEZONE defaults to America/Los_Angeles, and TIMESTAMP_TZ keeps the writer's offset forever. Every instant was correct; every kick-off printed nine hours early. An evening Ligue 1 match at 11:45.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "What dbt is doing here. Everything upstream WRITES facts; dbt only ever READS them and shapes them for the dashboard. That split is enforced by schema: tasks own CORE, dbt owns ANALYTICS, and dbt has no write path into CORE at all -- so a full-refresh cannot drop a table a task paid an API call for. dbt run builds 8 models in dependency order, working out that order itself from the ref() and source() calls -- nobody maintains a list. Six staging models are VIEWS: they rename and lightly filter facts the pipeline already wrote, so a table would just be a second copy that can go stale. The two gold models are TABLES, because the dashboard reads them on every page load and a view would re-scan five million settled rows each time someone opens the URL. Last run: 8 of 8 OK, about 12 seconds. dbt test then runs 31 assertions against what was just built -- 26 generic ones declared in YAML (not_null, unique, accepted_values, and relationships for foreign keys) plus 5 singular tests, which are just SQL that must return zero rows. The generic ones catch schema drift. The singular ones are the interesting half, because every one of them was written the day a bug got past everything else: arbitrage_is_physically_plausible exists because the first run reported a 7.83x arbitrage; slip_legs_are_unique because two joins were fanning legs out; odds_are_real_prices because a book publishes 0.00 for a suspended outcome; slip_rates_are_proportions because pooling two teams' histories produced a rate above 1; gold_is_not_empty because an empty table passes every column test ever written. Order matters in the DAG: dbt_run, then dbt_test, then the LLM tasks -- the model is the one step that costs money per run, and paying for a verdict on data that failed its own tests is the wrong order. And one test taught its own lesson: a relationships test on market_base_id used to FAIL the run, but dim_market is the crosswalk and native books legitimately price outside it, so it is warn-severity now. A test encodes an assumption; if the assumption is wrong the test is too. The point to make about dbt: generic tests catch schema drift; the singular tests here each encode a bug that a plausible number got past. 'Arbitrage physically plausible' exists because the first run reported a 7.83× arbitrage. ~1:30.");
  head(s, "Layer 8 · Transformation", "dbt: the models, and the tests that earn their keep");
  layer(s, {
    what: [
      [A("8 models"), T(": staging views over the facts, gold tables for market efficiency and slip-leg history, an observed-outcome-label model built from what the books themselves publish.")],
      [A("30 tests"), T(", four of them singular and written after a bug: "), I("arbitrage is physically plausible, slip legs are unique, odds are real prices, gold is not empty"), T(".")],
      "on-run-start pins the session timezone; QUALIFY dedups every append-only source before it is joined.",
    ],
    tools: ["dbt-snowflake", "singular tests", "macros"],
    gotcha: "bronze_slip_payload is keyed on payload hash, so a slip re-fetched after losing a leg is stored again. 598 rows for 409 slips → 2,247 duplicated legs, fed to the LLM with repeats. Fix: read the table by the key it actually has.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:45",
    "Snowflake Cortex was the plan and would have been free; it refuses AI functions on trial accounts, verified in the console. Moving to an orchestrated OpenAI task put the LLM behind a swappable boundary. Emphasise the design rule: the model narrates numbers it did not compute. ~1:45.");
  head(s, "Layer 9 · AI", "An LLM that may use nothing but our data");
  layer(s, {
    what: [
      [A("Slip verdicts:"), T(" every copied booking slip, leg by leg, with the price and how often that exact market has landed for that side. Opens with a bold sentence: "), I("\"a 1-in-232 shot.\"")],
      [A("Fixture briefs:"), T(" three paragraphs — what the market thought, what both sides had been doing, where they disagree.")],
      [A("All arithmetic in the warehouse."), T(" The model is handed finished numbers and forbidden from computing, from outside knowledge, and from reading \"copied by 8,462\" as a probability.")],
      "The cache key hashes the evidence, not just the subject.",
    ],
    tools: ["OpenAI gpt-4o-mini", "Cortex — blocked on trial", "signature cache"],
    label: "Silently wrong, twice",
    gotcha: "A verdict said \"recent form of 90%\" beside a table reading \"no history\" — legs unchanged, evidence gone, cache key unchanged. Later, round(1.19)=1 produced \"1 in 1 … extremely rare\" for an 84%-likely slip. Both fixed in SQL, not in the prompt.",
  });
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "If there is time, this is the live-demo cue: open the dashboard, click a deep dive, show the price chart with the kick-off line. Otherwise the screenshot carries it. ~1:30.");
  head(s, "Layer 10 · Serving & orchestration", "Streamlit and Airflow");
  card(s, 0.5, 1.5, 4.7, 3.55);
  s.addImage({ path: SHOTS + "01-overview.png", x: 0.6, y: 1.6, w: 4.5, h: 3.35, sizing: { type: "cover", w: 4.5, h: 3.35 } });
  card(s, 5.4, 1.5, 4.1, 3.55);
  h3(s, "Streamlit", 5.6, 1.6, 3.8);
  bullets(s, [
    "Two pages: market signals, and a deep dive per fixture with the LLM brief, every book's price over time, and both sides' record.",
    [T("Queries cached "), A("10 min"), T(" because Snowflake auto-suspends at 5 — an uncached load would bill a minute of credit for a page nobody is reading.")],
    "Standalone: imports no pipeline code, so Community Cloud installs from a 5-line requirements.txt.",
  ], 5.6, 1.9, 3.75, 1.6, 9);
  h3(s, "Airflow", 5.6, 3.5, 3.8);
  bullets(s, [
    [T("One DAG, "), A("12 tasks"), T(", LocalExecutor. Every task is a BashOperator against a separate pipeline venv — the DAG file imports nothing from the project.")],
    "Never backfills: every task reads the current state of bronze, not a partition.",
  ], 5.6, 3.8, 3.75, 1.2, 9);
}
{
  const s = slide("Act 2 · What I built · 1:30",
    "Two concrete stories, both measured this week. The panel will like that the 40s → <1s figure came from the watcher's own log, not a benchmark. ~1:30.");
  head(s, "Layer 11 · Performance", "Indexes: the one query shape you never write");
  card(s, 0.5, 1.5, 4.4, 3.55);
  h3(s, "The index that holds bronze up", 0.7, 1.62, 4.0);
  s.addShape(pres.ShapeType.rect, { x: 0.7, y: 1.95, w: 4.0, h: 0.6, fill: { color: C.surf2 }, line: { color: C.line, width: 0.5 } });
  s.addText("idx_bep_event_book_write\n  (event_id, bookmaker, write_time DESC)", { x: 0.8, y: 1.98, w: 3.8, h: 0.55, fontFace: F.m, fontSize: 9, color: C.info, margin: 0, isTextBox: true, valign: "middle" });
  bullets(s, [
    "Every read drives on event_id — the leading column. \"Latest per book\" is one ordered walk.",
    [A("Never"), T(" filter on bookmaker or write_time alone: no index leads with them, so the planner falls back to a sequential scan over the BYTEA payload bodies. That is the query that froze the production host mid-tournament.")],
  ], 0.7, 2.7, 4.0, 2.3, 9.5);
  card(s, 5.1, 1.5, 4.4, 3.55);
  h3(s, "Measured, not assumed", 5.3, 1.62, 4.0);
  s.addTable([
    [{ text: "POLL SHAPE", options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7 } }, { text: "FIXTURES", options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7 } }, { text: "TIME", options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7 } }],
    ["event_id = ANY(…), 3-day window", { text: "1,368", options: { fontFace: F.m } }, { text: "≈ 40 s", options: { fontFace: F.m, color: C.wrong } }],
    ["event_id = ANY(…), 6-hour window", { text: "100", options: { fontFace: F.m } }, { text: "< 1 s", options: { fontFace: F.m, color: C.loud } }],
  ], { x: 5.3, y: 1.95, w: 4.0, colW: [2.2, 0.9, 0.9], fontFace: F.b, fontSize: 9, color: C.ink, fill: { color: C.surf }, border: { type: "solid", color: C.line, pt: 0.5 }, margin: 0.05 });
  para(s, "Same index, same query. A large enough array tips the planner into scanning the table anyway. Bound the key set and the index serves it.", 5.3, 3.15, 4.0, 0.75, 9.5);
  s.addText([T("In Snowflake the equivalent discipline is the "), A("MERGE key"), T(": every write matches on a natural key so re-runs converge instead of duplicate.")], { x: 5.3, y: 3.95, w: 4.0, h: 0.9, fontFace: F.b, fontSize: 9.5, color: C.ink, valign: "top", margin: 0, isTextBox: true });
}

// ================================================================ ACT 3 · LEARNED
{
  const s = slide("Act 3 · What I learned · 1:30",
    "Answer the two questions from Act 1 here, explicitly. 72 unit tests, 30 dbt tests, ruff and mypy clean — say it once. ~1:30.");
  head(s, "Results", "What the pipeline actually found");
  stat(s, 0.5, 1.5, 2.85, 1.85, "4", "true surebets in 6.9M payloads. First run said 174 — every extra one was a crosswalk defect.");
  stat(s, 3.55, 1.5, 2.85, 1.85, "5.96M", "settled team-market results; 32,057 price changes replayed with 0 unparseable payloads", 34);
  stat(s, 6.6, 1.5, 2.85, 1.85, "393", "booking slips judged; 86% of legs reach a settled history");
  card(s, 0.5, 3.5, 4.4, 1.5);
  s.addText([A("Arbitrage is approximately zero — and that is the answer."), T(" The best surebet, 1.0396, is on first-half corners: the obscure market where soft books pay least attention.")], { x: 0.7, y: 3.6, w: 4.0, h: 1.3, fontFace: F.b, fontSize: 10.5, color: C.ink, valign: "top", margin: 0, isTextBox: true });
  card(s, 5.1, 3.5, 4.4, 1.5);
  s.addText([A("Popularity and soundness are unrelated."), T(" 5,739 people copied a slip with a 1-in-411,956 chance. The most-copied slip on the same fetch was a sane 1-in-13.")], { x: 5.3, y: 3.6, w: 4.0, h: 1.3, fontFace: F.b, fontSize: 10.5, color: C.ink, valign: "top", margin: 0, isTextBox: true });
}
{
  const s = slide("Act 3 · What I learned · 2:30",
    "Give this slide the most time. Walk two or three rows, not all seven. End on the last sentence of the chip — it is the one line you want the panel to repeat back. ~2:30.");
  head(s, "The thesis", "Silent wrongness beats loud failure — and it is worse", 30);
  const H = (t) => ({ text: t, options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7 } });
  s.addTable([
    [H("WHAT IT LOOKED LIKE"), H("WHAT IT WAS"), H("WHAT CAUGHT IT")],
    ["174 arbitrages, best 7.83×", "O/U ladder collapsed into one market; asian handicap not normalised; suspended prices read as live", "\"Is 783% of stake physically possible?\""],
    ["Slip cards: \"0 with history\"", "Rolling-window NULLs MERGEd over 3,226 fixtures' team ids", "The dashboard put a claim beside its evidence"],
    ["Kick-off 11:45", "Session timezone America/Los_Angeles; TIMESTAMP_TZ keeps the offset", "\"Ligue 1 does not kick off at lunch\""],
    ["\"Won 0/10\" for every side", "Compared RESULT to \"win\"; the column holds \"W\"", "Reading the rendered page"],
    ["\"1 in 1 … extremely rare\"", "round(1.19)=1; model invented a frequency from the copy count", "Reading the rendered sentence"],
    ["\"outcome 12\"", "Taxonomy names only what it can settle; id 12 is over in one market and 1-2 in another", "Refusing to guess; using the book's own label"],
    ["A price collapsing to 0.00", "One book emits 0.00 for suspended outcomes; in the engine 1/0=∞ and the market is never flagged", "A chart axis reaching zero when told not to"],
    ["published=73 failed=0, exit 0", "Broker advertised localhost, so the containerised producer connected to itself; delivery reports ignored", "The topic's high-water mark had not moved"],
    ["\"arbitrage 0.9897\" on 2 legs", "Both legs were the SAME book -- its own overround, admitted by the 0.98 threshold", "A dbt relationships test failed on the orphan market"],
  ], { x: 0.5, y: 1.32, w: 9, colW: [2.2, 4.4, 2.4], fontFace: F.b, fontSize: 8, color: C.ink, fill: { color: C.surf }, border: { type: "solid", color: C.line, pt: 0.5 }, margin: 0.035, valign: "top" });
  gotcha(s, 0.5, 4.32, 9, 0.78, "wrong", "The pattern",
    "Every one produced a plausible number. None raised an error, failed a test, or changed a row count. The loud failures — unknown_status, the NOT NULL at write — were the cheap ones. Put claims next to their evidence, and ask \"is this physically possible?\" before \"does it run?\".");
}
{
  const s = slide("Act 3 · What I learned · 2:00",
    "Dense on purpose — this is the slide the panel will photograph. Do not read it. Pick the one story per column you tell best (JAVA_HOME, winutils, Cortex) and let the rest sit. ~2:00.");
  head(s, "Troubleshooting the toolchain", "What each tool taught me, the hard way");
  const col = (x, blocks) => {
    card(s, x, 1.42, 2.9, 3.68);
    let y = 1.52;
    blocks.forEach(([title, items, h]) => { h3(s, title, x + 0.15, y, 2.6); bullets(s, items, x + 0.15, y + 0.26, 2.6, h, 7.5); y += h + 0.26; });
  };
  col(0.5, [
    ["Kafka / Redpanda", ["Two detectors need two consumer groups, or one gets every message and the other none -- silently.", "produce() only QUEUES. Check the delivery report and flush()'s return, or a dead broker looks like success.", "The ADVERTISED address is what clients reconnect to; one listener cannot serve host and container.", "message.max.bytes is enforced client-side. Raise it on producer, topic, cluster AND consumer together.", "Reset the topic before measuring a parser change."], 2.05],
    ["Git & shell", ["Piping tests through tail masked exit codes -- a broken build was committed. set -o pipefail.", "Two sessions in one working tree commit each other's files."], 0.95],
  ]);
  col(3.55, [
    ["Airflow", ["Keep the pipeline venv separate from Airflow's own; the DAG file imports nothing from the project.", "--env-file .env handed the host's JAVA_HOME to a Linux container: the JVM never started.", "A compose edit is not a running container -- the logs mount was declared but unapplied for days.", "standalone does NOT respawn the webserver: a 120 s gunicorn timeout killed the UI for eight hours."], 1.75],
    ["Spark", ["Windows has no Hadoop temp dir without winutils.exe -- run Spark in the image, never the host.", "Bound a long job (3,000 rows) before the 80-minute run."], 1.05],
  ]);
  col(6.6, [
    ["Snowflake", ["PRIMARY KEY is documentation; MERGE on natural keys.", "Session timezone is LA by default; TIMESTAMP_TZ keeps the writer's offset.", "Cortex AI functions refuse trial accounts."], 1.35],
    ["dbt", ["Separate who writes what: CORE vs ANALYTICS. A full-refresh must never drop paid work.", "A relationships test encodes an assumption -- dim_market is the crosswalk, so warn, do not fail.", "QUALIFY every append-only source before joining it."], 1.45],
  ]);
}
{
  const s = slide("Act 3 · What I learned · 1:30",
    "Say the acronyms out loud in full once — the brief asked for it — then give one example each. The crosswalk rebuild story is a good one because it was your correction: 'you overcomplicate things.' ~1:30.");
  head(s, "Principles", "Four acronyms, applied — not recited");
  card(s, 0.5, 1.5, 4.4, 3.55);
  h3(s, "YAGNI — You Aren't Gonna Need It", 0.7, 1.62, 4.0);
  para(s, "A 16-hour phased crosswalk rebuild was proposed; the crosswalk CSV turned out to exist. Build for the case in front of you.", 0.7, 1.95, 4.0, 0.9, 10, C.ink);
  h3(s, "KISS — Keep It Simple, Stupid", 0.7, 3.05, 4.0);
  para(s, "The watcher is one polling loop composing four existing functions — no new queue, no new service. Spark was skipped for the tick extractor: 5,000 payloads is a for-loop.", 0.7, 3.38, 4.0, 1.5, 10, C.ink);
  card(s, 5.1, 1.5, 4.4, 3.55);
  h3(s, "DRY — Don't Repeat Yourself", 5.3, 1.62, 4.0);
  para(s, "One MERGE builder for every write; one crosswalk parses both the live signals and the historical ticks, so a chart that disagrees with a signal is a real disagreement.", 5.3, 1.95, 4.0, 0.9, 10, C.ink);
  h3(s, "SOLID", 5.3, 3.05, 4.0);
  para(s, "Single responsibility · Open/closed · Liskov substitution · Interface segregation · Dependency inversion. The LLM sits behind a swappable boundary (Cortex → OpenAI was a one-file change). Prompt building is separate from the API call because the prompt is where the decisions are.", 5.3, 3.38, 4.0, 1.5, 10, C.ink);
}
{
  const s = slide("Act 3 · What I learned · 1:45",
    "This slide protects you. Panels reward candour about AI use far more than they penalise it, and the reused/new split is exactly the README's honesty statement. Fill the monolith placeholder with the specific repo. ~1:45.");
  head(s, "Method & authorship", "AI-assisted development, honestly");
  card(s, 0.5, 1.5, 4.4, 3.55);
  h3(s, "How the work was done", 0.7, 1.62, 4.0);
  bullets(s, [
    [T("A "), A("spec-driven coordinator → implementor → verifier"), T(" loop. I wrote the architecture, the specifications and the acceptance checks; AI implementors wrote much of the code against them; I reviewed and verified.")],
    [T("Used deliberately to "), A("improve a monolith I had already built"), T(" — the API-Football ingestor's README calls itself \"a disciplined rebuild of an earlier monolith\".")],
    "Every finding, trap and convention is written down in a 900-line FINDINGS.md as part of the work, not after it.",
  ], 0.7, 1.95, 4.0, 2.2, 9.5);
  placeholder(s, "[WHICH REPO, WHAT AI CHANGED]", 0.7, 4.35, 4.0, 0.5);
  s.addShape(pres.ShapeType.roundRect, { x: 5.1, y: 1.5, w: 4.4, h: 3.55, fill: { color: C.surf }, line: { color: C.acc, width: 1.25 }, rectRadius: 0.06 });
  h3(s, "What is mine, reused, and new", 5.3, 1.62, 4.0);
  bullets(s, [
    [A("Reused:"), T(" the bronze collection layer, the matcher, the five parsers, the 236-row crosswalk, the arbitrage/EV engine, the 4,874-line settlement engine.")],
    [A("New in the capstone:"), T(" producer, consumers, watcher, warehouse schema, Spark jobs, slip ingest, every dbt model, both LLM tasks, CI, the DAG, the dashboard.")],
    [A("Collector:"), T(" inherited v1; my Mongo→Postgres migration, team-ID extraction, event-ID collision fix.")],
  ], 5.3, 1.95, 4.0, 3.0, 9.5);
}
{
  const s = slide("Act 3 · What I learned · 1:00",
    "The Snowflake figure is read from ACCOUNT_USAGE, not estimated. If asked what 9.27 credits would cost off-trial: roughly tens of dollars at list pricing for an X-Small — do not quote a rate you cannot cite. ~1:00.");
  head(s, "Costs", "What it cost to run");
  card(s, 0.5, 1.5, 5.6, 3.55);
  const Hh = (t) => ({ text: t, options: { bold: true, color: C.muted, fontFace: F.m, fontSize: 7 } });
  s.addTable([
    [Hh("COMPONENT"), Hh("USAGE"), Hh("COST")],
    ["Snowflake", "9.27 credits over 3 active days · 0.05 GB stored", "$0 — 30-day trial credit"],
    ["OpenAI gpt-4o-mini", "a few hundred calls (slip verdicts + fixture briefs)", "≈ $0.30 (estimate)"],
    ["Redpanda, Airflow, Postgres, Spark", "local containers", "$0"],
    ["Streamlit Community Cloud", "—", "$0"],
    ["Cold parquet lake", "43 GB local disk", "$0"],
  ], { x: 0.7, y: 1.65, w: 5.2, colW: [1.7, 2.2, 1.3], fontFace: F.b, fontSize: 9, color: C.ink, fill: { color: C.surf }, border: { type: "solid", color: C.line, pt: 0.5 }, margin: 0.05, valign: "top" });
  card(s, 6.3, 1.5, 3.2, 3.55);
  h3(s, "Local machine power", 6.5, 1.62, 2.8);
  placeholder(s, "[YOUR INPUT] kWh / average watts during the 80-minute Spark runs and the collection window; the tool you measured with", 6.5, 1.95, 2.8, 1.3);
  para(s, "Snowflake Cortex would have been $0 and was the original plan; it is unavailable on trial accounts, so the LLM moved to OpenAI — which, as a side effect, made it swappable.", 6.5, 3.4, 2.8, 1.5, 9);
}
{
  const s = slide("Act 3 · What I learned · 1:00",
    "Keep it short; the panel's questions start here. The first bullet is a genuine open bug and saying so is stronger than hiding it. ~1:00.");
  head(s, "Next", "What I would do next");
  card(s, 0.5, 1.5, 4.4, 3.55);
  h3(s, "Known gaps, deliberately recorded", 0.7, 1.62, 4.0);
  bullets(s, [
    "The 0.00-odds parser defect still reaches the arbitrage engine upstream — a false-negative source. Fix belongs in the markets repo beside the other four.",
    "msport's parser likely shares sportybet's suspended-market bug; unchecked.",
    "Bookmaker names are shown; a repo-wide scrub was built, reverted on request, and documented for re-use.",
  ], 0.7, 1.95, 4.0, 3.0, 10);
  card(s, 5.1, 1.5, 4.4, 3.55);
  h3(s, "Roadmap", 5.3, 1.62, 4.0);
  bullets(s, [
    "Postgres LISTEN/NOTIFY to make the watcher event-driven instead of 20-second polling.",
    "Arbitrage-over-time: the as-of join that shows the line crossing 1.0.",
    "Events, lineups and players from API-Football into the same medallion.",
    "AWS, IaC, Iceberg, Great Expectations — absent by decision, not omission.",
  ], 5.3, 1.95, 4.0, 3.0, 10);
}
{
  const s = slide("Close · 0:30",
    "Invite questions on the failures specifically — you have better answers there than anyone expects. ~0:30. Total running time with the suggested timings is about 33 minutes; trim the Nigeria or World Cup slide to 45 seconds each to land at 30.");
  s.addText("Arbibet", { x: 0.5, y: 1.55, w: 8, h: 0.3, fontFace: F.m, fontSize: 11, color: C.muted, charSpacing: 1, margin: 0, isTextBox: true });
  s.addText("ASK ME WHAT\nWENT WRONG.", { x: 0.5, y: 1.9, w: 9, h: 1.7, fontFace: F.d, fontSize: 60, bold: true, color: C.ink, margin: 0, isTextBox: true, valign: "top" });
  para(s, "Every number in this deck is measured, and the ones that mean less than they look are labelled.", 0.5, 3.65, 6.5, 0.6, 14, C.ink);
  placeholder(s, "[DASHBOARD URL]", 0.5, 4.5, 2.6, 0.36);
  placeholder(s, "[REPO URL]", 3.25, 4.5, 2.2, 0.36);
}

if (n !== TOTAL) throw new Error(`slide count ${n} != ${TOTAL}`);
pres.writeFile({ fileName: OUT }).then(() => console.log("wrote", OUT, "slides:", n));
