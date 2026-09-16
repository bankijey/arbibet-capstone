"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useLocalText, writeLocal } from "./stores";
import type { Flag } from "./types";

const LOCAL = "arbibet-flags";
const RESTORED = "arbibet-flags-restored";
export const flagKey = (eventId: string, marketId: string, book: string) => `${eventId}|${marketId}|${book}`;

function parse<T>(text: string, fallback: T): T {
  try {
    return JSON.parse(text) as T;
  } catch {
    return fallback;
  }
}

/**
 * Leg flags ("not on the bookmaker's site"). Shared through /api/flags when the
 * site has Blob storage configured; always mirrored in this browser, so a flag
 * works immediately and still works without the API. `initial` are the flags
 * the snapshot carried (the old dashboard's, from Snowflake); those cannot be
 * deleted from here, so restoring one records it as restored locally.
 */
export function useFlags(initial: Flag[]) {
  const localText = useLocalText(LOCAL, "[]");
  const restoredText = useLocalText(RESTORED, "[]");
  const local = useMemo(() => parse<Flag[]>(localText, []), [localText]);
  const restored = useMemo(() => new Set(parse<string[]>(restoredText, [])), [restoredText]);
  const [shared, setShared] = useState<Flag[]>([]);
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    fetch("/api/flags", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (!body) return;
        setEnabled(Boolean(body.enabled));
        setShared(body.flags ?? []);
      })
      .catch(() => {});
  }, []);

  const flags = useMemo(() => {
    const all = new Map<string, Flag>();
    for (const f of [...initial, ...shared, ...local]) {
      const key = flagKey(f.eventId, f.marketId, f.book);
      if (!restored.has(key)) all.set(key, f);
    }
    return [...all.values()];
  }, [initial, shared, local, restored]);
  const keys = useMemo(() => new Set(flags.map((f) => flagKey(f.eventId, f.marketId, f.book))), [flags]);

  const setFlag = useCallback(
    async (eventId: string, marketId: string, book: string, active: boolean) => {
      const key = flagKey(eventId, marketId, book);
      const others = local.filter((f) => flagKey(f.eventId, f.marketId, f.book) !== key);
      writeLocal(
        LOCAL,
        JSON.stringify(active ? [...others, { eventId, marketId, book, flaggedAt: new Date().toISOString() }] : others),
      );
      const gone = new Set(restored);
      if (active) gone.delete(key);
      else gone.add(key);
      writeLocal(RESTORED, JSON.stringify([...gone]));
      if (!enabled) return;
      const response = await fetch("/api/flags", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ eventId, marketId, book, active }),
      }).catch(() => null);
      if (response?.ok) setShared((await response.json()).flags ?? []);
    },
    [enabled, local, restored],
  );

  return { flags, keys, setFlag, shared: enabled };
}
