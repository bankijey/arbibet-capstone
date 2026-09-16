"use client";

import clsx from "clsx";
import { useMemo, useState } from "react";

import { BacktestChart, BookBars, OddsChart } from "@/components/charts";
import {
  Badge,
  BetLink,
  BookName,
  Callout,
  Empty,
  Section,
  Segmented,
  Select,
  SubHeading,
  Table,
  Td,
  Tabs,
  Th,
} from "@/components/ui";
import { RESULT_LABELS, SIZING_LABELS, TIMING_LABELS } from "@/lib/betting";
import { int, kickoff, marketLabel, num, pct, shortDay } from "@/lib/format";
import type { EvRow, Live } from "@/lib/types";
import { flagKey, type useFlags } from "@/lib/useFlags";
import { isUpcoming } from "@/lib/useNow";

import { Count, Offered } from "./Arbitrage";

type Flags = ReturnType<typeof useFlags>;
const outcomeKey = (r: EvRow) => `${r.eventId}|${r.marketId}|${r.outcomeId}`;

export function EvSection({ data, now, flags }: { data: Live["ev"]; now: number; flags: Flags }) {
  const [threshold, setThreshold] = useState(0.015);
  const [tab, setTab] = useState<"upcoming" | "past">("upcoming");

  const rows = useMemo(
    () => data.rows.filter((r) => r.ev >= threshold && !flags.keys.has(flagKey(r.eventId, r.marketId, r.book))),
    [data.rows, threshold, flags.keys],
  );
  const upcoming = useMemo(
    () => rows.filter((r) => isUpcoming(r.kickoffAt, now) && r.isFresh === true),
    [rows, now],
  );
  const past = useMemo(() => rows.filter((r) => !isUpcoming(r.kickoffAt, now)), [rows, now]);

  return (
    <Section id="ev" title="Positive EV" subtitle="Prices that beat a published probability, and what betting them did">
      <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg bg-surface-2 px-3 py-2.5">
        <label htmlFor="ev-threshold" className="text-sm text-muted">
          Show EV from
        </label>
        <input
          id="ev-threshold"
          type="range"
          min={0}
          max={0.1}
          step={0.005}
          value={threshold}
          onChange={(e) => setThreshold(Number(e.target.value))}
          className="w-48"
        />
        <b className="w-14 tabular-nums">{threshold.toFixed(3)}</b>
        <span className="text-xs text-faint">
          Expected value per unit staked: 0.015 means the price beats its probability by 1.5% of the stake. The backtest
          uses every positive EV.
        </span>
      </div>

      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "upcoming", label: <>Upcoming <Count n={upcoming.length} /></> },
          { value: "past", label: <>Past <Count n={past.length} /></> },
        ]}
      />

      {tab === "upcoming" ? (
        <>
          <p className="mb-3 max-w-3xl text-sm leading-relaxed text-muted">
            Fixtures that have not kicked off: every outcome whose price beat its probability by at least{" "}
            {threshold.toFixed(3)}. The probability is the most recent one sportybet or msport published, within five
            minutes of the price. <b>On the book now</b> is its latest payload, checked every pipeline run.
          </p>
          {upcoming.length === 0 ? (
            <Empty>No EV of {threshold.toFixed(3)} or more on a fixture still to be played.</Empty>
          ) : (
            <>
              <EvTable rows={upcoming} flags={flags} />
              <EvChart rows={upcoming} data={data} threshold={threshold} played={false} now={now} />
            </>
          )}
        </>
      ) : (
        <>
          <SubHeading note="Only sportybet and msport publish a probability alongside their prices, so every other book's EV is measured against a borrowed one.">
            Positive EV by book
          </SubHeading>
          <BookBars rows={data.byBook.map((b) => ({ book: b.book, value: b.signals }))} label="EV signals" />
          <BacktestPanel backtest={data.backtest} />
          <SubHeading>
            Past opportunities · {int(past.length)} at {threshold.toFixed(3)} or more
          </SubHeading>
          {past.length === 0 ? (
            <Empty>None.</Empty>
          ) : (
            <>
              <EvTable rows={past} played />
              <EvChart rows={past} data={data} threshold={threshold} played now={now} />
            </>
          )}
        </>
      )}
    </Section>
  );
}

function Verdict({ verdict }: { verdict: string | null }) {
  if (!verdict) return <span className="text-faint">pending</span>;
  const tone = verdict === "won" || verdict === "half_win" ? "good" : verdict === "lost" || verdict === "half_loss" ? "bad" : "neutral";
  return <Badge tone={tone}>{RESULT_LABELS[verdict] ?? verdict}</Badge>;
}

function EvTable({ rows, flags, played }: { rows: EvRow[]; flags?: Flags; played?: boolean }) {
  return (
    <Table maxHeight={460}>
      <thead>
        <tr>
          <Th>Fixture</Th>
          <Th>Market</Th>
          <Th>Outcome</Th>
          <Th>Book</Th>
          <Th right>EV</Th>
          <Th right>Odds at detection</Th>
          <Th right>Last recorded</Th>
          {!played && <Th>On the book now</Th>}
          <Th right>Implied p</Th>
          <Th>Comparable</Th>
          <Th right>Time lapse (s)</Th>
          <Th>Kick-off</Th>
          <Th>Priced at</Th>
          {played && <Th>Result</Th>}
          <Th />
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={`${outcomeKey(r)}|${r.book}|${r.detectedAt}|${i}`} className="hover:bg-surface-2">
            <Td className="min-w-44 font-medium">{r.fixture}</Td>
            <Td className="whitespace-nowrap">{marketLabel(r.market, r.line)}</Td>
            <Td>{r.outcome}</Td>
            <Td><BookName book={r.book} /></Td>
            <Td right className="font-semibold text-good">{num(r.ev, 3)}</Td>
            <Td right>{num(r.odds)}</Td>
            <Td right>{num(r.latestOdds)}</Td>
            {!played && <Td><Offered leg={r} /></Td>}
            <Td right>{pct(r.impliedP, 1)}</Td>
            <Td>{r.comparable ?? "—"}</Td>
            <Td right>{int(r.timeLapseSeconds)}</Td>
            <Td className="whitespace-nowrap">{kickoff(r.kickoffAt)}</Td>
            <Td className="whitespace-nowrap">{kickoff(r.detectedAt)}</Td>
            {played && <Td><Verdict verdict={r.verdict} /></Td>}
            <Td>
              <div className="flex items-center justify-end gap-2">
                <BetLink url={r.url} />
                {flags && (
                  <button
                    title="Not on the bookmaker's site: hide this leg's signals"
                    onClick={() => flags.setFlag(r.eventId, r.marketId, r.book, true)}
                    className="grid h-6 w-6 place-items-center rounded-md text-faint hover:bg-bad-soft hover:text-bad"
                  >
                    ✕
                  </button>
                )}
              </div>
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function EvChart({
  rows,
  data,
  threshold,
  played,
  now,
}: {
  rows: EvRow[];
  data: Live["ev"];
  threshold: number;
  played: boolean;
  now: number;
}) {
  const groups = useMemo(() => {
    const map = new Map<string, { key: string; row: EvRow; best: number; marks: EvRow[] }>();
    for (const r of rows) {
      const key = `${outcomeKey(r)}|${r.book}`;
      const g = map.get(key) ?? { key, row: r, best: r.ev, marks: [] };
      g.best = Math.max(g.best, r.ev);
      g.marks.push(r);
      map.set(key, g);
    }
    return [...map.values()].sort((a, b) => b.best - a.best);
  }, [rows]);
  const [picked, setPicked] = useState<string>("");
  const g = groups.find((x) => x.key === picked) ?? groups[0];
  if (!g) return null;
  const prices = data.prices[outcomeKey(g.row)];
  const end = played && g.row.kickoffAt ? new Date(g.row.kickoffAt).getTime() : now;
  const clipped = prices
    ? Object.fromEntries(
        Object.entries(prices).map(([book, pts]) => [book, pts.filter((p) => new Date(p[0]).getTime() <= end)]),
      )
    : null;

  return (
    <div className="mt-5">
      <Select
        label="Chart an opportunity"
        value={g.key}
        onChange={setPicked}
        options={groups.map((x) => ({
          value: x.key,
          label: `${x.row.fixture} — ${marketLabel(x.row.market, x.row.line)} · ${x.row.outcome} @ ${x.row.book} (best EV ${x.best.toFixed(3)})`,
        }))}
      />
      <div className="mt-3">
        {clipped && Object.values(clipped).some((p) => p.length) ? (
          <>
            <OddsChart
              prices={clipped}
              highlight={g.row.book}
              marks={g.marks.map((m) => ({ at: m.detectedAt, odds: m.odds, ev: m.ev, book: m.book }))}
              kickoffAt={g.row.kickoffAt}
              end={end}
              markLabel={`EV ≥ ${threshold.toFixed(3)}`}
            />
            <p className="mt-1 text-xs text-faint">
              Every book&apos;s price for {g.row.outcome} in its site&apos;s colour, each line its ~10 largest moves and
              held until changed; {g.row.book} in bold. Diamonds: EV at or above the threshold. Star: the last recorded
              price, {played ? "at kick-off" : "now"}.
            </p>
          </>
        ) : (
          <Empty>No price history extracted for this outcome yet.</Empty>
        )}
      </div>
    </div>
  );
}

function BacktestPanel({ backtest }: { backtest: Live["ev"]["backtest"] }) {
  const [timing, setTiming] = useState("first");
  const ranked = useMemo(
    () =>
      [...backtest.table]
        .map((r) => ({ ...r, score: (r.final - 100) / Math.max(r.max_drawdown, 0.01) / 100 }))
        .sort((a, b) => b.score - a.score),
    [backtest.table],
  );

  return (
    <>
      <SubHeading
        note={
          backtest.opportunities
            ? `${backtest.opportunities} settled opportunities (fixture, market, outcome) from ${shortDay(backtest.from!)} to ${shortDay(backtest.to!)}. Starting bankroll 100. Stakes come from cash not tied up in unsettled bets; each bet settles two hours after kick-off. Kelly stakes EV / (odds − 1) of the bankroll, growth-optimal only if the probability is right; here it is a bookmaker's, margin included.`
            : undefined
        }
      >
        Backtest: what if every past positive EV had been bet?
      </SubHeading>
      {!backtest.opportunities ? (
        <Empty>No positive-EV opportunity has settled yet.</Empty>
      ) : (
        <>
          <Segmented
            label="Take the price"
            value={timing}
            onChange={setTiming}
            options={Object.entries(TIMING_LABELS).map(([value, label]) => ({ value, label }))}
          />
          <div className="mt-3">
            <BacktestChart curves={backtest.curves[timing] ?? {}} labels={SIZING_LABELS} />
          </div>
          {ranked[0] && (
            <div className="mt-3">
              <Callout tone="good">
                <b>
                  Best risk-adjusted: {SIZING_LABELS[ranked[0].sizing]}, taking the price{" "}
                  {TIMING_LABELS[ranked[0].timing].toLowerCase()}
                </b>{" "}
                — bankroll 100 → {ranked[0].final.toFixed(0)}, worst drawdown {pct(ranked[0].max_drawdown)}. On{" "}
                {backtest.opportunities} opportunities this is a reading, not a law.
              </Callout>
            </div>
          )}
          <div className="mt-3">
            <Table maxHeight={420}>
              <thead>
                <tr>
                  <Th>Sizing</Th>
                  <Th>Timing</Th>
                  <Th right>Bets</Th>
                  <Th right>Hit rate</Th>
                  <Th right>Staked</Th>
                  <Th right>ROI on stakes</Th>
                  <Th right>Final bankroll</Th>
                  <Th right>Max drawdown</Th>
                </tr>
              </thead>
              <tbody>
                {ranked.map((r) => (
                  <tr key={`${r.timing}-${r.sizing}`} className={clsx(r.timing === timing && "bg-accent-soft/40")}>
                    <Td className="font-medium">{SIZING_LABELS[r.sizing]}</Td>
                    <Td className="whitespace-nowrap">{TIMING_LABELS[r.timing]}</Td>
                    <Td right>{r.bets}</Td>
                    <Td right>{pct(r.hit_rate)}</Td>
                    <Td right>{num(r.staked, 1)}</Td>
                    <Td right className={clsx((r.roi ?? 0) >= 0 ? "text-good" : "text-bad")}>{pct(r.roi, 1, true)}</Td>
                    <Td right className="font-semibold">{num(r.final, 1)}</Td>
                    <Td right>{pct(r.max_drawdown)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        </>
      )}
    </>
  );
}
