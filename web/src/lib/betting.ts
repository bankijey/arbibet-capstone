// Books in their own sites' colours. bet9ja and ilotbet are both green, so
// ilotbet is also dashed: colour alone is never the only difference. msport's
// yellow is drawn with a dark outline so it reads on a light background.
export const BOOK_COLOURS: Record<string, string> = {
  bet9ja: "#0D7B3C",
  sportybet: "#E41827",
  livescorebet: "#FF6B00",
  msport: "#FFCA27",
  ilotbet: "#1FCB6E",
};
export const BOOK_DASHED = new Set(["ilotbet"]);
export const BOOK_OUTLINED = new Set(["msport"]);
export const bookColour = (book: string) => BOOK_COLOURS[book] ?? "#64748b";

// The detector's freshness bar: legs priced further apart were never on sale
// together (FINDINGS 13g).
export const MAX_LEG_SPREAD_SECONDS = 300;

export interface StakeSplit {
  arbitrage: number;
  fractions: number[];
}

/**
 * Split a stake so every outcome returns the same.
 *
 * arbitrage = 1 / sum(1 / odds); stake share i = (1 / odds_i) / sum(1 / odds).
 * Every outcome then returns stake x arbitrage. Null when any price is unusable.
 */
export function stakeSplit(odds: number[]): StakeSplit | null {
  if (!odds.length || odds.some((o) => !Number.isFinite(o) || o <= 1)) return null;
  const inverse = odds.map((o) => 1 / o);
  const total = inverse.reduce((a, b) => a + b, 0);
  return { arbitrage: 1 / total, fractions: inverse.map((i) => i / total) };
}

export const SIZING_LABELS: Record<string, string> = {
  flat: "Flat 1% of start",
  fixed_2pct: "2% of bankroll",
  kelly: "Full Kelly",
  half_kelly: "Half Kelly",
  quarter_kelly: "Quarter Kelly",
};

export const TIMING_LABELS: Record<string, string> = {
  first: "When first seen",
  best: "At its best EV",
  last: "Last before kick-off",
};

export const RESULT_LABELS: Record<string, string> = {
  won: "won",
  lost: "lost",
  half_win: "half won",
  half_loss: "half lost",
  push: "push",
  void: "void",
};
