import { Badge, Card, Empty, Metric, Table, Td, Th } from "@/components/ui";
import { RESULT_LABELS } from "@/lib/betting";
import { int, num, pct, shortDay } from "@/lib/format";
import type { Dive, TeamMatch } from "@/lib/types";

const toNumber = (v: string | number | null | undefined) => {
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number.parseFloat(String(v).replace("%", ""));
  return Number.isFinite(n) ? n : null;
};

const mean = (values: (number | null)[]) => {
  const present = values.filter((v): v is number => v !== null && Number.isFinite(v));
  return present.length ? present.reduce((a, b) => a + b, 0) / present.length : null;
};

// label, accessor, decimals per match
const NUMBERS: [string, (m: TeamMatch) => number | null, number][] = [
  ["xG", (m) => m.xg, 2],
  ["xGA", (m) => m.xga, 2],
  ["Shots", (m) => m.shots, 0],
  ["On target", (m) => m.shotsOn, 0],
  ["Passes", (m) => m.passes, 0],
  ["Poss %", (m) => toNumber(m.possession), 0],
  ["Corners", (m) => m.corners, 0],
  ["Saves", (m) => m.saves, 0],
  ["Blocked", (m) => m.blocked, 0],
  ["Fouls", (m) => m.fouls, 0],
  ["Cards", (m) => (m.yellow ?? 0) + (m.red ?? 0), 0],
];

const shotAcc = (m: TeamMatch) => (m.shots ? (m.shotsOn ?? 0) / m.shots : null);
const passAcc = (m: TeamMatch) => {
  const p = toNumber(m.passesPct);
  if (p !== null) return p / 100;
  return m.passes ? (m.passesAccurate ?? 0) / m.passes : null;
};

export function TeamForm({ team }: { team: Dive["teams"][number] }) {
  const matches = team.matches;
  if (!matches.length) {
    return (
      <div className="mt-4">
        <div className="mb-2 font-semibold">
          {team.name} <span className="font-normal text-muted">· {team.role} side</span>
        </div>
        <Empty>No settled history for this side.</Empty>
      </div>
    );
  }
  // 'W', not 'win': the first dashboard compared against "win" and every side read Won 0/10.
  const wins = matches.filter((m) => m.result === "W").length;
  const scored = mean(matches.map((m) => m.goalsFor));
  const conceded = mean(matches.map((m) => m.goalsAgainst));
  const clean = matches.filter((m) => m.goalsAgainst === 0).length;
  const xg = mean(matches.map((m) => m.xg));

  return (
    <Card className="mt-4 p-4 sm:p-5">
      <div className="mb-4 flex flex-wrap items-baseline gap-2">
        <span className="text-base font-semibold">{team.name}</span>
        <span className="text-sm text-muted">{team.role} side</span>
        <span className="ml-auto flex gap-1">
          {[...matches].reverse().map((m, i) => (
            <span
              key={i}
              title={`${m.opponent ?? ""} ${m.goalsFor ?? ""}-${m.goalsAgainst ?? ""}`}
              className={`grid h-6 w-6 place-items-center rounded text-xs font-semibold ${
                m.result === "W" ? "bg-good-soft text-good" : m.result === "L" ? "bg-bad-soft text-bad" : "bg-surface-2 text-muted"
              }`}
            >
              {m.result ?? "?"}
            </span>
          ))}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 lg:grid-cols-7">
        <Metric small label="Won" value={`${wins}/${matches.length}`} />
        <Metric small label="Scored" value={num(scored, 1)} hint="Goals per match." />
        <Metric small label="Conceded" value={num(conceded, 1)} />
        <Metric small label="Clean sheets" value={`${clean}/${matches.length}`} />
        <Metric small label="xG" value={num(xg, 2)} hint="API-Football populates xG for some leagues only." />
        <Metric small label="Shot accuracy" value={pct(mean(matches.map(shotAcc)))} hint="Shots on target over total shots, per match, averaged." />
        <Metric small label="Pass accuracy" value={pct(mean(matches.map(passAcc)))} />
      </div>
      <div className="mt-4">
        <Table>
          <thead>
            <tr>
              <Th>Date</Th>
              <Th>Competition</Th>
              <Th>Opponent</Th>
              <Th>H/A</Th>
              <Th right>Score</Th>
              {NUMBERS.slice(0, 4).map(([label]) => (
                <Th key={label} right>{label}</Th>
              ))}
              <Th right>Shot acc</Th>
              <Th right>Passes</Th>
              <Th right>Pass acc</Th>
              {NUMBERS.slice(5).map(([label]) => (
                <Th key={label} right>{label}</Th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="bg-accent-soft/40 font-medium">
              <Td>Mean · last {matches.length}</Td>
              <Td />
              <Td />
              <Td />
              <Td right>
                {num(scored, 1)}-{num(conceded, 1)}
              </Td>
              {NUMBERS.slice(0, 4).map(([label, get, digits]) => (
                <Td key={label} right>{num(mean(matches.map(get)), Math.max(digits, 1))}</Td>
              ))}
              <Td right>{pct(mean(matches.map(shotAcc)))}</Td>
              <Td right>{num(mean(matches.map((m) => m.passes)), 1)}</Td>
              <Td right>{pct(mean(matches.map(passAcc)))}</Td>
              {NUMBERS.slice(5).map(([label, get, digits]) => (
                <Td key={label} right>{num(mean(matches.map(get)), Math.max(digits, 1))}</Td>
              ))}
            </tr>
            {matches.map((m, i) => (
              <tr key={i} className="hover:bg-surface-2">
                <Td className="whitespace-nowrap">{shortDay(m.date)}</Td>
                <Td className="max-w-48 truncate text-muted" title={m.competition ?? ""}>{m.competition ?? ""}</Td>
                <Td className="whitespace-nowrap">{m.opponent ?? ""}</Td>
                <Td>{m.isHome ? "H" : "A"}</Td>
                <Td right>
                  <span
                    className={
                      m.result === "W" ? "font-semibold text-good" : m.result === "L" ? "font-semibold text-bad" : "font-semibold"
                    }
                  >
                    {int(m.goalsFor)}-{int(m.goalsAgainst)}
                  </span>
                </Td>
                {NUMBERS.slice(0, 4).map(([label, get, digits]) => (
                  <Td key={label} right>{num(get(m), digits)}</Td>
                ))}
                <Td right>{pct(shotAcc(m))}</Td>
                <Td right>{int(m.passes)}</Td>
                <Td right>{pct(passAcc(m))}</Td>
                {NUMBERS.slice(5).map(([label, get, digits]) => (
                  <Td key={label} right>{num(get(m), digits)}</Td>
                ))}
              </tr>
            ))}
          </tbody>
        </Table>
      </div>
    </Card>
  );
}

export function SettledMarkets({ rows }: { rows: Dive["settled"] }) {
  if (!rows.length) {
    return (
      <div className="mt-3">
        <Empty>No settled market history for either side.</Empty>
      </div>
    );
  }
  return (
    <div className="mt-3">
      <Table maxHeight={480}>
        <thead>
          <tr>
            <Th>Side</Th>
            <Th>Market</Th>
            <Th>Period</Th>
            <Th>Pick</Th>
            <Th right>Landed</Th>
            <Th>Rate</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="hover:bg-surface-2">
              <Td className="font-medium">{r.side}</Td>
              <Td>{r.market}</Td>
              <Td>{r.period}</Td>
              <Td>{r.pick}</Td>
              <Td right>
                {r.landed}/{r.of}
              </Td>
              <Td>
                <RateBar value={r.landed / r.of} />
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

function RateBar({ value }: { value: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-28 overflow-hidden rounded-full bg-surface-2">
        <div className="h-full rounded-full bg-accent" style={{ width: `${value * 100}%` }} />
      </div>
      <span className="w-10 tabular-nums text-muted">{pct(value)}</span>
    </div>
  );
}

export function Punters({ punters }: { punters: Dive["punters"] }) {
  if (!punters || !punters.picks.length) {
    return (
      <div className="mt-3">
        <Empty>No booking slip in the corpus names this fixture.</Empty>
      </div>
    );
  }
  const top = punters.picks[0];
  return (
    <>
      <Card className="mt-3 grid grid-cols-2 gap-4 p-4 sm:grid-cols-5 sm:p-5">
        <Metric small label="Slips" value={int(punters.slips)} hint="Distinct slips naming this fixture." />
        <Metric small label="Copied" value={int(punters.copies)} hint="Copies across those slips." />
        <Metric small label="Median copies" value={int(punters.medianCopies)} />
        <Metric small label="Distinct picks" value={int(punters.picks.length)} />
        <Metric small label="Most backed" value={pct(top.slips / punters.slips)} delta={top.pick} />
      </Card>
      <div className="mt-3">
        <Table maxHeight={480}>
          <thead>
            <tr>
              <Th>Pick</Th>
              <Th right>Slips</Th>
              <Th>Share of slips</Th>
              <Th right>Copies</Th>
              <Th right>Median odds</Th>
              <Th right>Last 10</Th>
              <Th>Result</Th>
            </tr>
          </thead>
          <tbody>
            {punters.picks.map((p) => (
              <tr key={p.pick} className="hover:bg-surface-2">
                <Td className="font-medium">{p.pick}</Td>
                <Td right>{int(p.slips)}</Td>
                <Td>
                  <RateBar value={p.slips / punters.slips} />
                </Td>
                <Td right>{int(p.copies)}</Td>
                <Td right>{num(p.medianOdds)}</Td>
                <Td right>{p.matches ? `${int(p.wins)}/${int(p.matches)}` : <span className="text-faint">no history</span>}</Td>
                <Td>
                  {p.result ? (
                    <Badge tone={p.result === "won" || p.result === "half_win" ? "good" : p.result === "lost" || p.result === "half_loss" ? "bad" : "neutral"}>
                      {RESULT_LABELS[p.result] ?? p.result}
                    </Badge>
                  ) : (
                    <span className="text-faint">—</span>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </div>
    </>
  );
}
