"use client";

import { useState } from "react";

import { MarketPanel } from "@/components/charts";
import { BookName, Select } from "@/components/ui";
import { int, marketLabel } from "@/lib/format";
import type { DiveMarket } from "@/lib/types";

export function MarketExplorer({ markets, kickoffAt }: { markets: DiveMarket[]; kickoffAt: string }) {
  // 1x2 first when the fixture has it: the busiest market is often a handicap line.
  const [picked, setPicked] = useState((markets.find((m) => m.name.toLowerCase() === "1x2") ?? markets[0]).marketId);
  const market = markets.find((m) => m.marketId === picked) ?? markets[0];
  const outcomes = Object.keys(market.series);
  const books = [...new Set(outcomes.flatMap((o) => Object.keys(market.series[o])))].sort();

  return (
    <div className="mt-3 rounded-xl border border-border bg-surface p-4 sm:p-5">
      <div className="flex flex-wrap items-end gap-4">
        <Select
          className="w-full sm:w-96"
          label="Market"
          value={market.marketId}
          onChange={setPicked}
          options={markets.map((m) => ({
            value: m.marketId,
            label: `${marketLabel(m.name, m.line)} (${int(m.ticks)} price changes)`,
          }))}
        />
        <div className="flex flex-wrap gap-3 pb-2 text-sm text-muted">
          {books.map((b) => (
            <BookName key={b} book={b} />
          ))}
        </div>
      </div>
      <div className={`mt-4 grid gap-5 ${outcomes.length > 1 ? "lg:grid-cols-2" : ""}`}>
        {outcomes.map((o) => (
          <MarketPanel key={`${market.marketId}-${o}`} title={o} prices={market.series[o]} kickoffAt={kickoffAt} />
        ))}
      </div>
      <p className="mt-3 text-xs leading-relaxed text-faint">
        {int(market.ticks)} price changes across {market.books} books, each book&apos;s line drawn as its ~10 largest
        moves. Ctrl + scroll to zoom, drag to pan. The dashed line is kick-off: to the left the books disagree about a match that has not
        happened, to the right they react to one that is. The {markets.length} busiest markets are charted.
      </p>
    </div>
  );
}
