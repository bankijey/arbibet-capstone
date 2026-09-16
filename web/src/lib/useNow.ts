"use client";

import { useEffect, useState } from "react";

/**
 * The clock, for deciding what is still "upcoming".
 *
 * Starts at the snapshot's own time so the server render and the first client
 * render agree (no hydration mismatch), then moves to the real clock and
 * ticks every minute. A snapshot is up to an hour old; a match that kicked off
 * since must not still be offered as a bet.
 */
export function useNow(initial: string): number {
  const [now, setNow] = useState(() => new Date(initial).getTime());
  useEffect(() => {
    const tick = () => setNow(Date.now());
    tick();
    const id = window.setInterval(tick, 60_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

export const isUpcoming = (iso: string | null | undefined, now: number) =>
  !!iso && new Date(iso).getTime() > now;
