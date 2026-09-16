// Every time on the site is Europe/Berlin, as in the pipeline and the old
// dashboard: bronze speaks Berlin, and mixing zones is how a chart's axis and
// its kick-off marker end up disagreeing.
export const TIMEZONE = "Europe/Berlin";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Parts in Berlin time, formatted by hand: Intl's en-GB month is "Sept", and
// its punctuation differs between Node and browsers, which would also make the
// server render and the hydrated page disagree.
const partsFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIMEZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  weekday: "long",
  hourCycle: "h23",
});

function berlin(iso: string) {
  const out: Record<string, string> = {};
  for (const p of partsFmt.formatToParts(new Date(iso))) out[p.type] = p.value;
  return {
    year: out.year,
    month: Number(out.month),
    day: out.day,
    time: `${out.hour}:${out.minute}`,
    weekday: out.weekday,
  };
}

/** 16 Sep 19:00 */
export const kickoff = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const b = berlin(iso);
  return `${b.day} ${MONTHS[b.month - 1]} ${b.time}`;
};
/** 19:00 */
export const clock = (iso: string | null | undefined) => (iso ? berlin(iso).time : "—");
/** Wednesday 16 September 2026, 19:00 */
export const fullDate = (iso: string) => {
  const b = berlin(iso);
  const month = new Date(Date.UTC(2000, b.month - 1, 1)).toLocaleString("en-GB", { month: "long", timeZone: "UTC" });
  return `${b.weekday} ${Number(b.day)} ${month} ${b.year}, ${b.time}`;
};
/** 16 Sep 26 */
export const shortDay = (iso: string) => {
  const b = berlin(iso);
  return `${b.day} ${MONTHS[b.month - 1]} ${b.year.slice(2)}`;
};
/** 2026-09-16 19:00 */
export const stamp = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const b = berlin(iso);
  return `${b.year}-${String(b.month).padStart(2, "0")}-${b.day} ${b.time}`;
};

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export const int = (value: number | null | undefined) => num(value, 0);

export function pct(value: number | null | undefined, digits = 0, signed = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const text = `${(value * 100).toFixed(digits)}%`;
  return signed && value > 0 ? `+${text}` : text;
}

export function signed(value: number, digits = 4): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function ago(iso: string | null | undefined, now: number): string {
  if (!iso) return "—";
  const minutes = Math.round((now - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export function marketLabel(market: string, line: string | null | undefined): string {
  return line ? `${market} ${line}` : market;
}
