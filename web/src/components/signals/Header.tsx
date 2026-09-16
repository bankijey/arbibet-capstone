"use client";

import { Card, Metric } from "@/components/ui";
import { ago, int, stamp } from "@/lib/format";
import type { Live } from "@/lib/types";
import { useNow } from "@/lib/useNow";

export function SignalsHeader({ generatedAt, asof, counts }: Pick<Live, "generatedAt" | "asof" | "counts">) {
  const now = useNow(generatedAt);
  return (
    <>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Cross-bookmaker market signals</h1>
          <p className="mt-1.5 max-w-2xl text-muted">
            Five Nigerian and international bookmakers, reconciled onto one market taxonomy. Every number is
            measured, and the ones that mean less than they look are labelled.
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm text-muted" title={`Snapshot ${stamp(generatedAt)}`}>
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-good opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-good" />
          </span>
          Updated {ago(generatedAt, now)}
        </div>
      </div>

      <Card className="mt-5 grid grid-cols-2 gap-x-4 gap-y-5 p-4 sm:grid-cols-3 sm:p-5 lg:grid-cols-5">
        <Metric
          label="True surebets"
          value={int(counts.surebets)}
          hint="Arbitrage above 1, priced before kick-off, every leg within five minutes of the others. All time; the ones still placeable are under Upcoming. Genuine surebets across public books barely exist."
        />
        <Metric label="Positive EV" value={int(counts.evSignals)} hint="Priced before kick-off. All time." />
        <Metric label="Price changes" value={int(counts.ticks)} hint="Every published price move, replayed." />
        <Metric label="Markets settled" value={int(counts.settled)} hint="Historical results, per team per market." />
        <Metric label="Slips analysed" value={int(counts.slips)} />
      </Card>
      <p className="mt-2 text-xs text-faint">
        Newest signal {stamp(asof.lastSignal)} · newest price {stamp(asof.lastTick)} · newest slip verdict{" "}
        {stamp(asof.lastSummary)} · all times Berlin
      </p>
    </>
  );
}
