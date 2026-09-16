import { revalidatePath } from "next/cache";

// Called by publish/snapshot.py after it uploads a new snapshot, so every page
// is regenerated on its next visit instead of waiting for the hourly fallback.
export async function POST(request: Request) {
  const secret = process.env.REVALIDATE_SECRET;
  if (!secret || request.headers.get("x-revalidate-secret") !== secret) {
    return Response.json({ error: "unauthorised" }, { status: 401 });
  }
  revalidatePath("/", "layout");
  return Response.json({ revalidated: true, at: new Date().toISOString() });
}
