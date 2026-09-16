import { Explainer } from "@/components/signals/Explainer";
import { SignalsHeader } from "@/components/signals/Header";
import { SignalsBody } from "@/components/signals/SignalsBody";
import { NoSnapshot } from "@/components/NoSnapshot";
import { loadLive } from "@/lib/data";

// Rendered from the snapshot and cached. The pipeline revalidates after each
// upload; the hour is only the fallback.
export const revalidate = 3600;

export default async function Page() {
  const live = await loadLive();
  if (!live) return <NoSnapshot />;
  return (
    <>
      <SignalsHeader generatedAt={live.generatedAt} asof={live.asof} counts={live.counts} />
      <Explainer example={live.example} freshness={live.freshness} />
      <SignalsBody generatedAt={live.generatedAt} arbitrage={live.arbitrage} ev={live.ev} />
    </>
  );
}
