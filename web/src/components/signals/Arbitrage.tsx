"use client";

import clsx from "clsx";
import { useMemo, useState } from "react";

import { ArbitrageChart, EfficiencyBars } from "@/components/charts";
import {
  Badge,
  BetLink,
  BookName,
  Callout,
  Card,
  Empty,
  Metric,
  Section,
  Select,
  SubHeading,
  Table,
  Tabs,
  Td,
  Th,
  Toggle,
} from "@/components/ui";
import { MAX_LEG_SPREAD_SECONDS, stakeSplit } from "@/lib/betting";
import { clock, int, kickoff, marketLabel, num, pct, signed } from "@/lib/format";
import type { ArbLeg, Live, Track } from "@/lib/types";
import { flagKey, type useFlags } from "@/lib/useFlags";
import { isUpcoming } from "@/lib/useNow";

import { ShareBar } from "./Explainer";

type Flags = ReturnType<typeof useFlags>;

interface MarketGroup {
  key: string;
  eventId: string;
  marketId: string;
  fixture: string;
  market: string;
  line: string | null;
  kickoffAt: string | null;
  legs: ArbLeg[];
  best: ArbLeg;
  detections: ArbLeg[]; // one row per signal
  newest: ArbLeg[]; // legs of the newest detection
  inPlay: boolean;
}

function group(legs: ArbLeg[]): MarketGroup[] {
  const map = new Map<string, ArbLeg[]>();
  for (const l of legs) {
    const key = `${l.eventId}|${l.marketId}`;
    map.set(key, [...(map.get(key) ?? []), l]);
  }
  return [...map.entries()].map(([key, rows]) => {
    const best = rows.reduce((a, b) => (b.arbitrage > a.arbitrage ? b : a));
    const bySignal = new Map<string, ArbLeg>();
    for (const r of rows) if (!bySignal.has(r.signalKey)) bySignal.set(r.signalKey, r);
    const detections = [...bySignal.values()].sort((a, b) => a.detectedAt.localeCompare(b.detectedAt));
    const newestKey = detections[detections.length - 1].signalKey;
    return {
      key,
      eventId: best.eventId,
      marketId: best.marketId,
      fixture: best.fixture,
      market: best.market,
      line: best.line,
      kickoffAt: best.kickoffAt,
      legs: rows,
      best,
      detections,
      newest: rows.filter((r) => r.signalKey === newestKey),
      inPlay: rows.some((r) => r.preMatch === false),
    };
  });
}

export function ArbitrageSection({ data, now, flags }: { data: Live["arbitrage"]; now: number; flags: Flags }) {
  const [tab, setTab] = useState<"upcoming" | "past">("upcoming");

  const visible = useMemo(() => {
    // A flag hides every detection that used that book on that market.
    const hidden = new Set(
      data.legs.filter((l) => flags.keys.has(flagKey(l.eventId, l.marketId, l.book))).map((l) => l.signalKey),
    );
    return data.legs.filter((l) => !hidden.has(l.signalKey));
  }, [data.legs, flags.keys]);

  const upcoming = useMemo(
    () =>
      group(visible.filter((l) => isUpcoming(l.kickoffAt, now) && l.preMatch === true)).sort(
        (a, b) => (a.kickoffAt ?? "").localeCompare(b.kickoffAt ?? ""),
      ),
    [visible, now],
  );
  const past = useMemo(
    () =>
      group(visible.filter((l) => !isUpcoming(l.kickoffAt, now))).sort((a, b) =>
        (b.kickoffAt ?? "").localeCompare(a.kickoffAt ?? ""),
      ),
    [visible, now],
  );

  return (
    <Section
      id="arbitrage"
      title="Arbitrage"
      subtitle="Surebets across books: what can still be placed, and the record"
      right={<HiddenFlags flags={flags} />}
    >
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
          <p className="mb-4 max-w-3xl text-sm leading-relaxed text-muted">
            Markets on fixtures that have not kicked off where a surebet has been detected. Each card says where it
            stands now, including below 1.0, whether every leg is still on the book, and how to split a stake at the
            odds you enter. Every leg of a detection was priced within five minutes of the others.
          </p>
          {upcoming.length === 0 ? (
            <Empty>
              No surebet on a fixture still to be played. That is the normal state: real surebets across public books
              are rare and close within minutes.
            </Empty>
          ) : (
            <div className="grid gap-4">
              {upcoming.map((g) => (
                <UpcomingCard key={g.key} group={g} track={data.tracks[g.key]} flags={flags} />
              ))}
            </div>
          )}
        </>
      ) : (
        <PastArbitrage past={past} data={data} />
      )}
    </Section>
  );
}

export function Count({ n }: { n: number }) {
  return <span className="ml-1 rounded-full bg-surface-2 px-1.5 py-0.5 text-xs tabular-nums text-muted">{n}</span>;
}

function HiddenFlags({ flags }: { flags: Flags }) {
  const [open, setOpen] = useState(false);
  if (!flags.flags.length) return null;
  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="rounded-md border border-border px-2.5 py-1 text-xs font-medium text-muted hover:text-text"
      >
        Hidden by flags ({flags.flags.length})
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-2 w-80 rounded-lg border border-border bg-surface p-2 shadow-xl">
          <p className="px-2 pb-2 text-xs text-muted">
            {flags.shared ? "Shared with every viewer." : "Kept in this browser."} Restore a leg to show its signals again.
          </p>
          {flags.flags.map((f) => (
            <div key={flagKey(f.eventId, f.marketId, f.book)} className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-surface-2">
              <div className="min-w-0 flex-1 text-xs">
                <BookName book={f.book} />
                <div className="truncate text-faint">
                  market {f.marketId} · {f.flaggedAt ? kickoff(f.flaggedAt) : ""}
                </div>
              </div>
              <button
                onClick={() => flags.setFlag(f.eventId, f.marketId, f.book, false)}
                className="rounded-md px-2 py-1 text-xs font-medium text-accent hover:bg-accent-soft"
              >
                Restore
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function UpcomingCard({ group: g, track, flags }: { group: MarketGroup; track: Track | undefined; flags: Flags }) {
  const [chart, setChart] = useState(false);
  const standing = track?.last;
  const withdrawn = g.newest.filter((l) => l.offered === false);
  const staleNow =
    standing && standing.arbitrage !== null && (standing.spreadSeconds ?? 0) > MAX_LEG_SPREAD_SECONDS;

  return (
    <Card className="flex flex-col p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-semibold leading-snug">{g.fixture}</div>
          <div className="text-sm text-muted">{marketLabel(g.market, g.line)}</div>
        </div>
        <Badge tone="accent">KO {kickoff(g.kickoffAt)}</Badge>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-3">
        <Metric
          small
          label="Best detected"
          value={num(g.best.arbitrage, 4)}
          delta={pct(g.best.arbitrage - 1, 2, true)}
          tone="good"
          hint={`Legs ${g.best.spreadSeconds}s apart, priced ${kickoff(g.best.detectedAt)}.`}
        />
        <Metric
          small
          label="Detections"
          value={g.detections.length}
          delta={`last ${kickoff(g.detections[g.detections.length - 1].detectedAt)}`}
        />
        {!standing ? (
          <Metric small label="Now" value="—" delta="not tracked yet" />
        ) : standing.arbitrage === null ? (
          <Metric
            small
            label="Now"
            value="no price"
            tone="warn"
            delta="not priceable"
            hint="The detector could not price this market across books at its last check: a book withdrew it, one book was pricing every outcome, or no probability was published."
          />
        ) : (
          <Metric
            small
            label="Now"
            value={num(standing.arbitrage, 4)}
            delta={`${signed(standing.arbitrage - g.best.arbitrage)} vs best`}
            tone={standing.arbitrage >= 1 ? "good" : "bad"}
          />
        )}
      </div>

      <div className="mt-3 space-y-2">
        {staleNow && (
          <Callout tone="warn">
            The now figure rests on prices {Math.round((standing!.spreadSeconds ?? 0) / 60)} minutes apart: real
            arithmetic, not a price anyone could take.
          </Callout>
        )}
        {withdrawn.length > 0 && (
          <Callout tone="bad">
            Not on the book now: {withdrawn.map((l) => `${l.outcome} at ${l.book}`).join(", ")}. The market was missing
            from its latest payload.
          </Callout>
        )}
      </div>

      <Sizing legs={g.newest} flags={flags} />

      <button
        onClick={() => setChart(!chart)}
        className="mt-4 flex items-center gap-1.5 self-start text-sm font-medium text-accent hover:underline"
      >
        <svg className={clsx("h-3.5 w-3.5 transition-transform", chart && "rotate-90")} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <path d="m9 6 6 6-6 6" />
        </svg>
        Arbitrage over time
      </button>
      {chart && (
        <div className="mt-2">
          <TrackChart track={track} group={g} nowLabel="now" />
        </div>
      )}
    </Card>
  );
}

function TrackChart({ track, group: g, nowLabel }: { track: Track | undefined; group: MarketGroup; nowLabel: string }) {
  if (!track || !track.points.length) {
    return <Empty>No tracked history for this market yet. It is rebuilt every pipeline run.</Empty>;
  }
  return (
    <>
      <ArbitrageChart
        points={track.points}
        detections={g.detections.map((d) => ({ at: d.detectedAt, arbitrage: d.arbitrage }))}
        kickoffAt={g.kickoffAt}
        nowLabel={nowLabel}
      />
      <p className="mt-1 text-xs text-faint">
        {int(track.total)} recorded changes, drawn as the ~10 largest. Red diamonds: surebet detections. Grey points:
        legs more than five minutes apart.
      </p>
    </>
  );
}

/** Editable odds per leg, the arbitrage they make, and how to split a stake. */
function Sizing({ legs, flags }: { legs: ArbLeg[]; flags: Flags }) {
  const start = (l: ArbLeg) =>
    l.offered && l.currentOdds ? l.currentOdds : (l.latestOdds ?? l.odds);
  const [odds, setOdds] = useState<string[]>(() => legs.map((l) => start(l).toFixed(2)));
  const [stake, setStake] = useState("100");
  const parsed = odds.map((o) => Number.parseFloat(o));
  const split = stakeSplit(parsed);
  const total = Math.max(0, Number.parseFloat(stake) || 0);

  return (
    <div className="mt-4">
      <p className="mb-2 text-xs text-muted">
        Legs of the newest detection ({kickoff(legs[0].detectedAt)}). Edit <b>Your odds</b> to what the site shows; flag
        ✕ if the market is not there.
      </p>
      <Table>
        <thead>
          <tr>
            <Th>Outcome</Th>
            <Th>Book</Th>
            <Th right>Detected</Th>
            <Th>On the book now</Th>
            <Th right>Your odds</Th>
            <Th right>Stake</Th>
            <Th right>Returns</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {legs.map((l, i) => {
            const share = split?.fractions[i] ?? 0;
            return (
              <tr key={`${l.outcomeId}-${l.book}`}>
                <Td className="font-medium">{l.outcome}</Td>
                <Td><BookName book={l.book} /></Td>
                <Td right>{num(l.odds)}</Td>
                <Td><Offered leg={l} /></Td>
                <Td right>
                  <input
                    inputMode="decimal"
                    value={odds[i]}
                    onChange={(e) => setOdds(odds.map((o, j) => (j === i ? e.target.value : o)))}
                    className="w-20 rounded-md border border-border bg-surface px-2 py-1 text-right tabular-nums outline-none focus:border-accent"
                  />
                </Td>
                <Td right>{split ? num(share * total) : "—"}</Td>
                <Td right>{split ? num(share * total * parsed[i]) : "—"}</Td>
                <Td>
                  <div className="flex items-center justify-end gap-2">
                    <BetLink url={l.url} />
                    <button
                      title="Not on the bookmaker's site: hide this leg's signals"
                      onClick={() => flags.setFlag(l.eventId, l.marketId, l.book, true)}
                      className="grid h-6 w-6 place-items-center rounded-md text-faint hover:bg-bad-soft hover:text-bad"
                    >
                      ✕
                    </button>
                  </div>
                </Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
      <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg bg-surface-2 px-3 py-2.5">
        <label className="flex items-center gap-2 text-sm text-muted">
          Total stake
          <input
            inputMode="decimal"
            value={stake}
            onChange={(e) => setStake(e.target.value)}
            className="w-24 rounded-md border border-border bg-surface px-2 py-1 text-right tabular-nums text-text outline-none focus:border-accent"
          />
        </label>
        {split ? (
          <div className="text-sm">
            <span className="text-muted">Arbitrage at these odds </span>
            <b className="tabular-nums">{num(split.arbitrage, 4)}</b>
            <span className={clsx("ml-2 font-medium tabular-nums", split.arbitrage >= 1 ? "text-good" : "text-bad")}>
              {signed((split.arbitrage - 1) * total, 2)} whatever lands ({pct(split.arbitrage - 1, 2, true)})
            </span>
          </div>
        ) : (
          <span className="text-sm text-muted">Enter a price above 1.00 for every leg to size the bet.</span>
        )}
      </div>
      {split && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
          {legs.map((l, i) => (
            <div key={i} className="flex items-center gap-2 text-xs text-muted">
              {l.outcome}
              <ShareBar value={split.fractions[i]} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Offered({ leg }: { leg: { offered: boolean | null; currentOdds: number | null; bookFiredAt: string | null } }) {
  if (leg.offered === null) return <span className="text-faint">not checked</span>;
  const when = leg.bookFiredAt ? ` · ${clock(leg.bookFiredAt)}` : "";
  return leg.offered ? (
    <span className="whitespace-nowrap text-good">yes at {num(leg.currentOdds)}<span className="text-faint">{when}</span></span>
  ) : (
    <span className="whitespace-nowrap text-bad">withdrawn<span className="text-faint">{when}</span></span>
  );
}

function PastArbitrage({ past, data }: { past: MarketGroup[]; data: Live["arbitrage"] }) {
  const [byLine, setByLine] = useState(false);
  const [picked, setPicked] = useState(past[0]?.key ?? "");
  const pickedGroup = past.find((g) => g.key === picked) ?? past[0];
  const efficiency = byLine ? data.efficiency.byLine : data.efficiency.byMarket;

  return (
    <>
      <SubHeading note="Overround left after shopping every book, 1 / arbitrage − 1, averaged over every signal recorded on the market. Zero means the best prices exactly cancel; red, below zero, is the surebet side; blue is the margin a punter still pays at the best prices. Near misses are used on purpose. Not per-bookmaker margin.">
        Market efficiency
      </SubHeading>
      <div className="mb-2">
        <Toggle checked={byLine} onChange={setByLine} label="Split each market by line" />
      </div>
      {efficiency.length ? <EfficiencyBars rows={efficiency} /> : <Empty>No signals yet.</Empty>}

      <SubHeading
        note={
          data.stale.n
            ? `${int(data.stale.n)} further signals above 1.0 are not shown anywhere, the largest ${num(data.stale.worst, 4)}: legs priced more than five minutes apart, never on sale together. Kept as the evidence for the freshness rule.`
            : undefined
        }
      >
        Past surebets
      </SubHeading>
      {past.length === 0 ? (
        <Empty>None.</Empty>
      ) : (
        <>
          <Table maxHeight={420}>
            <thead>
              <tr>
                <Th>Fixture</Th>
                <Th>Market</Th>
                <Th>Kick-off</Th>
                <Th right>Best detected</Th>
                <Th right>Detections</Th>
                <Th right>At kick-off</Th>
                <Th>Timing</Th>
              </tr>
            </thead>
            <tbody>
              {past.map((g) => {
                const atKickoff = data.tracks[g.key]?.last.arbitrage ?? null;
                return (
                  <tr
                    key={g.key}
                    onClick={() => setPicked(g.key)}
                    className={clsx("cursor-pointer hover:bg-surface-2", pickedGroup?.key === g.key && "bg-accent-soft/60")}
                  >
                    <Td className="font-medium">{g.fixture}</Td>
                    <Td>{marketLabel(g.market, g.line)}</Td>
                    <Td className="whitespace-nowrap">{kickoff(g.kickoffAt)}</Td>
                    <Td right>{num(g.best.arbitrage, 4)}</Td>
                    <Td right>{g.detections.length}</Td>
                    <Td right title="Where the detector's replay stood at kick-off">{num(atKickoff, 4)}</Td>
                    <Td>{g.inPlay ? <Badge tone="warn">in-play</Badge> : <Badge>pre-match</Badge>}</Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
          <p className="mt-1 text-xs text-faint">Select a row to chart it.</p>
          {pickedGroup && (
            <div className="mt-4">
              <Select
                label="Arbitrage over time for"
                value={pickedGroup.key}
                onChange={setPicked}
                options={past.map((g) => ({
                  value: g.key,
                  label: `${g.fixture} — ${marketLabel(g.market, g.line)} (best ${num(g.best.arbitrage, 4)})`,
                }))}
              />
              <div className="mt-3">
                <TrackChart track={data.tracks[pickedGroup.key]} group={pickedGroup} nowLabel="at kick-off" />
              </div>
            </div>
          )}
        </>
      )}
    </>
  );
}
