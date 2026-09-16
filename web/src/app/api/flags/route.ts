import { put } from "@vercel/blob";

import type { Flag } from "@/lib/types";

// Viewer flags: "this leg is not on the bookmaker's site". Shared by every
// viewer, so kept in one small public Blob file rather than a browser.
//
// Deliberately not Snowflake: a write there resumes the warehouse (a minute
// of billing at least) for one row. A flag write is one Blob operation.
export const dynamic = "force-dynamic";

const BASE = process.env.SNAPSHOT_BASE_URL?.replace(/\/$/, "");
const TOKEN = process.env.BLOB_READ_WRITE_TOKEN;
const NAME = "flags.json";

async function readFlags(): Promise<Flag[]> {
  if (!BASE) return [];
  const response = await fetch(`${BASE}/${NAME}?t=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) return [];
  return (await response.json()) as Flag[];
}

export async function GET() {
  return Response.json({ enabled: Boolean(BASE && TOKEN), flags: await readFlags() });
}

export async function POST(request: Request) {
  if (!BASE || !TOKEN) return Response.json({ error: "flags are not configured" }, { status: 501 });
  const body = (await request.json()) as { eventId?: string; marketId?: string; book?: string; active?: boolean };
  const { eventId, marketId, book, active } = body;
  const valid = (v: unknown) => typeof v === "string" && v.length > 0 && v.length < 100;
  if (!valid(eventId) || !valid(marketId) || !valid(book) || typeof active !== "boolean") {
    return Response.json({ error: "bad flag" }, { status: 400 });
  }
  const current = await readFlags();
  const others = current.filter((f) => !(f.eventId === eventId && f.marketId === marketId && f.book === book));
  const flags = active
    ? [...others, { eventId: eventId!, marketId: marketId!, book: book!, flaggedAt: new Date().toISOString() }]
    : others;
  await put(NAME, JSON.stringify(flags), {
    access: "public",
    token: TOKEN,
    addRandomSuffix: false,
    allowOverwrite: true,
    cacheControlMaxAge: 60,
    contentType: "application/json",
  });
  return Response.json({ enabled: true, flags });
}
