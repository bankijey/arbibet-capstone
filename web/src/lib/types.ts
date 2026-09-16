// The snapshot contract written by publish/snapshot.py. Timestamps are ISO UTC.

export type Point = [string, number | null, ...(number | null)[]];

export interface Live {
  generatedAt: string;
  asof: { lastSignal: string | null; lastTick: string | null; lastSummary: string | null };
  counts: { surebets: number; evSignals: number; ticks: number; settled: number; slips: number };
  example: {
    fixture: string;
    market: string;
    line: string | null;
    detectedAt: string;
    spreadSeconds: number;
    legs: { outcome: string; book: string; odds: number }[];
  } | null;
  freshness: { spread: string; signals: number; surebets: number }[];
  arbitrage: {
    legs: ArbLeg[];
    tracks: Record<string, Track>;
    stale: { n: number; worst: number | null };
    efficiency: { byMarket: Efficiency[]; byLine: Efficiency[] };
    flags: Flag[];
  };
  ev: {
    rows: EvRow[];
    prices: Record<string, Record<string, Point[]>>;
    byBook: { book: string; signals: number; meanEv: number }[];
    backtest: Backtest;
  };
  slips: {
    popular: PopularFixture[];
    cards: SlipCard[];
    legs: Record<string, SlipLeg[]>;
    waiting: number;
  };
  dives: Record<string, Dive>;
  archivedDays: string[];
}

export interface Availability {
  latestOdds: number | null;
  offered: boolean | null;
  currentOdds: number | null;
  bookFiredAt: string | null;
}

export interface ArbLeg extends Availability {
  signalKey: string;
  eventId: string;
  marketId: string;
  outcomeId: string;
  fixture: string;
  market: string;
  line: string | null;
  outcome: string;
  book: string;
  odds: number;
  url: string | null;
  arbitrage: number;
  spreadSeconds: number;
  detectedAt: string;
  kickoffAt: string | null;
  preMatch: boolean | null;
}

export interface Track {
  total: number;
  points: Point[]; // [at, arbitrage, spreadSeconds]
  last: { at: string; arbitrage: number | null; spreadSeconds: number | null };
}

export interface Efficiency {
  label: string;
  signals: number;
  meanOverround: number;
}

export interface Flag {
  eventId: string;
  marketId: string;
  book: string;
  flaggedAt: string | null;
}

export interface EvRow extends Availability {
  eventId: string;
  marketId: string;
  outcomeId: string;
  fixture: string;
  market: string;
  line: string | null;
  outcome: string;
  book: string;
  odds: number;
  url: string | null;
  impliedP: number | null;
  comparable: string | null;
  timeLapseSeconds: number | null;
  ev: number;
  kickoffAt: string | null;
  detectedAt: string;
  isFresh: boolean | null;
  verdict: string | null;
}

export interface BacktestRow {
  timing: string;
  sizing: string;
  bets: number;
  hit_rate: number | null;
  staked: number;
  profit: number;
  roi: number | null;
  final: number;
  max_drawdown: number;
}

export interface Backtest {
  opportunities: number;
  from?: string;
  to?: string;
  table: BacktestRow[];
  curves: Record<string, Record<string, Point[]>>;
}

export interface PopularFixture {
  eventId: string;
  fixture: string;
  tournament: string | null;
  kickoffAt: string;
  slips: number;
  follows: number | null;
}

export interface SlipCard {
  shareCode: string;
  followedTimes: number | null;
  legs: number;
  legsWithHistory: number;
  summary: string;
  legCount: number | null;
  firstKickoff: string | null;
  lastKickoff: string | null;
  won: number | null;
  lost: number | null;
  combinedOdds: number | null;
}

export interface SlipLeg {
  eventId: string;
  home: string;
  away: string;
  market: string;
  pick: string;
  odds: number;
  kickoffAt: string | null;
  historyWins: number | null;
  historyMatches: number | null;
  resolution: string | null;
  url: string | null;
  matchStatus: string | null;
  score: string | null;
  playedTime: string | null;
}

export interface Dive {
  eventId: string;
  home: string;
  away: string;
  tournament: string | null;
  kickoffAt: string;
  hasTeams: boolean;
  brief: { summary: string; model: string; markets: number; sides: number; generatedAt: string } | null;
  result: { summary: string; model: string; generatedAt: string } | null;
  markets: DiveMarket[];
  teams: { name: string; role: string; matches: TeamMatch[] }[];
  settled: { side: string; market: string; period: string; pick: string; landed: number; of: number }[];
  punters: {
    slips: number;
    copies: number;
    medianCopies: number;
    picks: {
      pick: string;
      slips: number;
      copies: number;
      medianOdds: number;
      wins: number | null;
      matches: number | null;
      result: string | null;
    }[];
  } | null;
}

export interface DiveMarket {
  marketId: string;
  name: string;
  line: string | null;
  ticks: number;
  books: number;
  series: Record<string, Record<string, Point[]>>; // outcome -> book -> points
}

export interface TeamMatch {
  date: string;
  competition: string | null;
  opponent: string | null;
  isHome: boolean | null;
  result: string | null;
  goalsFor: number | null;
  goalsAgainst: number | null;
  xg: number | null;
  xga: number | null;
  possession: string | number | null;
  corners: number | null;
  shots: number | null;
  shotsOn: number | null;
  passes: number | null;
  passesAccurate: number | null;
  passesPct: string | number | null;
  saves: number | null;
  blocked: number | null;
  fouls: number | null;
  yellow: number | null;
  red: number | null;
}

export interface Archive {
  day: string;
  generatedAt: string;
  dives: Record<string, Dive>;
}
