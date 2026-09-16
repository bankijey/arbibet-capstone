"use client";

import { SpreadBars } from "@/components/charts";
import { BookName, Section, SubHeading, Table, Td, Th } from "@/components/ui";
import { stakeSplit } from "@/lib/betting";
import { int, kickoff, marketLabel, num, pct } from "@/lib/format";
import type { Live } from "@/lib/types";

export function Explainer({ example, freshness }: Pick<Live, "example" | "freshness">) {
  const split = example ? stakeSplit(example.legs.map((l) => l.odds)) : null;
  const inverseSum = example ? example.legs.reduce((a, l) => a + 1 / l.odds, 0) : 0;
  return (
    <Section title="How an arbitrage is calculated" subtitle="The formula, a worked example, and what the detector refuses" defaultOpen={false}>
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-3 text-sm leading-relaxed text-muted">
          <p>
            For one market, take the <b className="text-text">best price per outcome across every book</b>, then
          </p>
          <div className="rounded-lg bg-surface-2 px-4 py-3 font-mono text-text">arbitrage = 1 / Σ (1 / best odds)</div>
          <p>
            Above <b className="text-text">1.0</b> is a surebet: backing every outcome at those prices returns more
            than the stake whatever happens. <span className="font-mono">1 / arbitrage − 1</span> is the overround left
            after shopping every book, the market-efficiency score.
          </p>
          <div className="rounded-lg bg-surface-2 px-4 py-3 font-mono text-text">stake on outcome i = (1 / oddsᵢ) / Σ (1 / odds)</div>
          <p>Every outcome then returns stake × arbitrage. The surebet cards do this for you, at odds you can edit.</p>
        </div>
        <div className="text-sm leading-relaxed text-muted">
          <p className="mb-2 font-medium text-text">What the detector refuses</p>
          <ul className="space-y-2">
            {[
              ["Legs from one book.", "Two outcomes of one book are that book's own margin, not a disagreement between books."],
              ["Legs priced more than five minutes apart.", "A snapshot holds each book's last published price, so one can be hours behind another."],
              ["Suspended markets.", "msport flags suspension on the market while leaving every outcome active; those prices are skipped."],
              ["Kicked-off fixtures.", "In play, books suspend and resume at different moments."],
              ["Markets without a usable probability.", "More than three outcomes, or published probabilities summing below 0.95."],
            ].map(([head, body]) => (
              <li key={head} className="flex gap-2">
                <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                <span>
                  <b className="text-text">{head}</b> {body}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {example && split && (
        <>
          <SubHeading
            note={`${example.fixture}, ${marketLabel(example.market, example.line)}, priced ${kickoff(example.detectedAt)}, legs ${int(example.spreadSeconds)}s apart.`}
          >
            Worked example: the newest surebet
          </SubHeading>
          <Table>
            <thead>
              <tr>
                <Th>Outcome</Th>
                <Th>Best book</Th>
                <Th right>Odds</Th>
                <Th right>1 / odds</Th>
                <Th>Stake share</Th>
              </tr>
            </thead>
            <tbody>
              {example.legs.map((l, i) => (
                <tr key={i}>
                  <Td>{l.outcome}</Td>
                  <Td><BookName book={l.book} /></Td>
                  <Td right>{num(l.odds)}</Td>
                  <Td right>{num(1 / l.odds, 4)}</Td>
                  <Td><ShareBar value={split.fractions[i]} /></Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <p className="mt-2 text-sm text-muted">
            Σ 1/odds = {num(inverseSum, 4)}, so arbitrage = 1 / {num(inverseSum, 4)} ={" "}
            <b className="text-text">{num(split.arbitrage, 4)}</b>: {pct(split.arbitrage - 1, 2, true)} on the total stake, whatever wins.
          </p>
        </>
      )}

      <SubHeading note="A snapshot holds each book's LAST published price, so one book can be an hour behind another. No signal with legs inside five minutes was ever implausible, and all 48 implausible ones sat above it, so the detector now refuses anything wider. Older rows predate that rule.">
        How simultaneous is a snapshot?
      </SubHeading>
      <div className="grid items-start gap-4 lg:grid-cols-[2fr_3fr]">
        <Table>
          <thead>
            <tr>
              <Th>Legs apart</Th>
              <Th right>Signals</Th>
              <Th right>Surebets</Th>
            </tr>
          </thead>
          <tbody>
            {freshness.map((r) => (
              <tr key={r.spread}>
                <Td>{r.spread}</Td>
                <Td right>{int(r.signals)}</Td>
                <Td right>{int(r.surebets)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
        <SpreadBars rows={freshness} />
      </div>
    </Section>
  );
}

export function ShareBar({ value }: { value: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-surface-2">
        <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </div>
      <span className="tabular-nums text-muted">{pct(value)}</span>
    </div>
  );
}
