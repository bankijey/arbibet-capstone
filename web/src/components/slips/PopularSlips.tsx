"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge, BetLink, Card, Empty, Prose, Section, Table, Tabs, Td, Th } from "@/components/ui";
import { RESULT_LABELS } from "@/lib/betting";
import { int, kickoff, num } from "@/lib/format";
import type { Live, SlipCard, SlipLeg } from "@/lib/types";
import { isUpcoming, useNow } from "@/lib/useNow";

import { Count } from "../signals/Arbitrage";

const SHOWN = 10;

export function PopularSlips({ generatedAt, slips }: { generatedAt: string; slips: Live["slips"] }) {
  const now = useNow(generatedAt);
  const [tab, setTab] = useState<"upcoming" | "played">("upcoming");
  const upcoming = useMemo(() => slips.cards.filter((c) => isUpcoming(c.lastKickoff, now)).slice(0, SHOWN), [slips.cards, now]);
  const played = useMemo(() => slips.cards.filter((c) => !isUpcoming(c.lastKickoff, now)).slice(0, SHOWN), [slips.cards, now]);
  const cards = tab === "upcoming" ? upcoming : played;

  return (
    <Section title="Popular slips, checked leg by leg" subtitle="The most-copied slips, each leg against how often that market has actually landed">
      <p className="mb-4 max-w-3xl text-sm leading-relaxed text-muted">
        The summary is written by gpt-4o-mini from the table beneath it; the numbers are the model&apos;s input, shown so
        you can judge the output. <b>Upcoming</b> slips still have a fixture to play; <b>played</b> slips are the record.
        Each tab is ranked by copies on its own. <b>Status</b> is the match as the books show it now; <b>Result</b> is how
        the leg settled.
      </p>
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "upcoming", label: <>Popular upcoming <Count n={upcoming.length} /></> },
          { value: "played", label: <>Popular played <Count n={played.length} /></> },
        ]}
      />
      {tab === "upcoming" && slips.waiting > 0 && (
        <p className="mb-3 text-sm text-muted">
          {int(slips.waiting)} upcoming slips have no verdict yet. Sixty are written each pipeline run, never-summarised
          and most-copied first.
        </p>
      )}
      {cards.length === 0 ? (
        <Empty>{tab === "upcoming" ? "No upcoming slip has a verdict yet." : "No played slips with a verdict."}</Empty>
      ) : (
        <div className="space-y-4">
          {cards.map((c) => (
            <SlipCardView key={c.shareCode} card={c} legs={slips.legs[c.shareCode] ?? []} now={now} />
          ))}
        </div>
      )}
      <p className="mt-5 text-xs leading-relaxed text-faint">
        Last 10 is recent form over at most ten matches and ignores opponent strength: it is not a probability for the
        fixture in hand. Slips are the still-bettable remnant, not the original bet: legs vanish at kick-off.
      </p>
    </Section>
  );
}

function status(leg: SlipLeg, now: number): { text: string; tone: "good" | "warn" | "neutral" | "accent" } {
  if (leg.matchStatus) {
    if (leg.matchStatus === "Not start") return { text: "not started", tone: "neutral" };
    const score = leg.score ? ` ${leg.score}` : "";
    if (leg.matchStatus === "Ended") return { text: `ended${score}`, tone: "neutral" };
    return { text: `live ${leg.matchStatus}${score}${leg.playedTime ? ` · ${leg.playedTime}` : ""}`, tone: "accent" };
  }
  return isUpcoming(leg.kickoffAt, now) ? { text: "not started", tone: "neutral" } : { text: "kicked off", tone: "warn" };
}

function SlipCardView({ card, legs, now }: { card: SlipCard; legs: SlipLeg[]; now: number }) {
  const [open, setOpen] = useState(false);
  const toPlay = legs.filter((l) => isUpcoming(l.kickoffAt, now)).length;
  const total = card.legCount ?? legs.length;
  const odds = card.combinedOdds;

  return (
    <Card className="overflow-hidden">
      <div className="grid gap-4 p-4 sm:p-5 md:grid-cols-[220px_1fr]">
        <div className="space-y-3 md:border-r md:border-border md:pr-5">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-1">
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-faint">Copied by</div>
              <div className="text-2xl font-semibold tabular-nums">{int(card.followedTimes)}</div>
            </div>
            {odds !== null && (
              <div title="Product of every leg: what the slip pays if all of it lands.">
                <div className="text-xs font-medium uppercase tracking-wide text-faint">Combined odds</div>
                <div className="text-2xl font-semibold tabular-nums">{odds < 100 ? num(odds) : int(odds)}</div>
              </div>
            )}
          </div>
          <div className="text-xs text-muted">
            <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono">{card.shareCode}</span> · {card.legs} legs ·{" "}
            {card.legsWithHistory} with history
          </div>
          <div>
            {legs.length === 0 ? (
              <Badge>no legs recorded</Badge>
            ) : toPlay === total ? (
              <Badge tone="good">upcoming · first KO {kickoff(card.firstKickoff)}</Badge>
            ) : toPlay ? (
              <Badge tone="warn">part-played · {toPlay} of {total} to play</Badge>
            ) : (
              <Badge>played · every leg kicked off</Badge>
            )}
          </div>
          {(card.won || card.lost) ? (
            <div className="text-xs text-muted">
              Settled so far: <span className="text-good">{card.won ?? 0} won</span> ·{" "}
              <span className="text-bad">{card.lost ?? 0} lost</span>
            </div>
          ) : null}
        </div>
        <div className="min-w-0">
          <Prose text={card.summary} />
          {legs.length > 0 && (
            <button onClick={() => setOpen(!open)} className="mt-3 text-sm font-medium text-accent hover:underline">
              {open ? "Hide" : "Show"} the {legs.length} legs behind this
            </button>
          )}
        </div>
      </div>
      {open && (
        <div className="border-t border-border p-3 sm:p-4">
          <Table maxHeight={480}>
            <thead>
              <tr>
                <Th>Fixture</Th>
                <Th>Kick-off</Th>
                <Th>Status</Th>
                <Th>Result</Th>
                <Th>Market</Th>
                <Th>Pick</Th>
                <Th right>Odds</Th>
                <Th right>Last 10</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {legs.map((l, i) => {
                const s = status(l, now);
                return (
                  <tr key={i} className="hover:bg-surface-2">
                    <Td className="min-w-44 font-medium">
                      <Link href={`/fixture/${l.eventId}`} className="hover:text-accent">
                        {l.home} v {l.away}
                      </Link>
                    </Td>
                    <Td className="whitespace-nowrap">{kickoff(l.kickoffAt)}</Td>
                    <Td><Badge tone={s.tone}>{s.text}</Badge></Td>
                    <Td>
                      {l.resolution ? (
                        <Badge tone={l.resolution.includes("win") || l.resolution === "won" ? "good" : l.resolution.includes("los") ? "bad" : "neutral"}>
                          {RESULT_LABELS[l.resolution] ?? l.resolution}
                        </Badge>
                      ) : (
                        <span className="text-faint">—</span>
                      )}
                    </Td>
                    <Td>{l.market}</Td>
                    <Td>{l.pick}</Td>
                    <Td right>{num(l.odds)}</Td>
                    <Td right>
                      {l.historyMatches ? `${int(l.historyWins)}/${int(l.historyMatches)}` : <span className="text-faint">no history</span>}
                    </Td>
                    <Td><BetLink url={l.url} /></Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}
