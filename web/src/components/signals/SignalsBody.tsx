"use client";

import type { Live } from "@/lib/types";
import { useFlags } from "@/lib/useFlags";
import { useNow } from "@/lib/useNow";

import { ArbitrageSection } from "./Arbitrage";
import { EvSection } from "./Ev";

/** Both signal sections share the clock and the viewer flags. */
export function SignalsBody({ generatedAt, arbitrage, ev }: { generatedAt: string } & Pick<Live, "arbitrage" | "ev">) {
  const now = useNow(generatedAt);
  const flags = useFlags(arbitrage.flags);
  return (
    <>
      <ArbitrageSection data={arbitrage} now={now} flags={flags} />
      <EvSection data={ev} now={now} flags={flags} />
    </>
  );
}
