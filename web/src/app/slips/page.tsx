import type { Metadata } from "next";

import { DeepDives } from "@/components/slips/DeepDives";
import { PopularSlips } from "@/components/slips/PopularSlips";
import { NoSnapshot } from "@/components/NoSnapshot";
import { loadLive } from "@/lib/data";

export const metadata: Metadata = { title: "Betting slips" };
export const revalidate = 3600;

export default async function SlipsPage() {
  const live = await loadLive();
  if (!live) return <NoSnapshot />;
  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Betting slips</h1>
      <p className="mt-1.5 max-w-2xl text-muted">
        Booking slips other people published and copied on msport, the fixtures they were built on, and how every leg
        is doing.
      </p>
      <DeepDives generatedAt={live.generatedAt} popular={live.slips.popular} />
      <PopularSlips generatedAt={live.generatedAt} slips={live.slips} />
    </>
  );
}
