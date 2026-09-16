import "server-only";

import { readFile } from "node:fs/promises";
import path from "node:path";
import { gunzipSync } from "node:zlib";
import { cache } from "react";

import type { Archive, Dive, Live } from "./types";

// Where the snapshots live. In production, the public Vercel Blob store the
// pipeline uploads to (SNAPSHOT_BASE_URL, e.g.
// https://<store>.public.blob.vercel-storage.com). In development, the
// directory publish/snapshot.py writes to.
//
// No page ever queries Snowflake. Pages are rendered from these files and
// cached; the pipeline calls /api/revalidate after each upload.
const BASE = process.env.SNAPSHOT_BASE_URL?.replace(/\/$/, "");
const LOCAL = path.join(process.cwd(), ".data");

async function read(name: string): Promise<Buffer | null> {
  if (BASE) {
    // The page itself is what is cached (ISR); this fetch runs only when a
    // page is regenerated. The files are over the 2 MB fetch-cache limit
    // once unzipped, so caching the fetch would not work anyway.
    const response = await fetch(`${BASE}/${name}`, { cache: "no-store" });
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
    return Buffer.from(await response.arrayBuffer());
  }
  try {
    return await readFile(path.join(LOCAL, name));
  } catch {
    return null;
  }
}

function parse<T>(body: Buffer): T {
  // Blob may hand back the bytes already decoded if a proxy honoured
  // content-encoding; gzip starts with 0x1f 0x8b.
  const raw = body[0] === 0x1f && body[1] === 0x8b ? gunzipSync(body) : body;
  return JSON.parse(raw.toString("utf-8")) as T;
}

/** The live snapshot, or null before the pipeline has published one. */
export const loadLive = cache(async (): Promise<Live | null> => {
  const body = await read("live.json.gz");
  return body ? parse<Live>(body) : null;
});

export const loadArchive = cache(async (day: string): Promise<Archive | null> => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const body = await read(`archive/${day}.json.gz`);
  return body ? parse<Archive>(body) : null;
});

export function berlinDay(iso: string): string {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Berlin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(iso));
}

/** A deep dive from the live snapshot, or its day's archive. */
export async function findDive(
  eventId: string,
): Promise<{ dive: Dive | null; archived: boolean; live: Live | null }> {
  const live = await loadLive();
  if (!live) return { dive: null, archived: false, live };
  if (live.dives[eventId]) return { dive: live.dives[eventId], archived: false, live };
  const fixture = live.slips.popular.find((p) => p.eventId === eventId);
  if (!fixture) return { dive: null, archived: false, live };
  const archive = await loadArchive(berlinDay(fixture.kickoffAt));
  return { dive: archive?.dives[eventId] ?? null, archived: true, live };
}
