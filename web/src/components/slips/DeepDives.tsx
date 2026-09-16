"use client";

import clsx from "clsx";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { Card, Empty, Section, SubHeading, Table, Td, Th } from "@/components/ui";
import { TIMEZONE, clock, int, kickoff, shortDay } from "@/lib/format";
import type { PopularFixture } from "@/lib/types";
import { isUpcoming, useNow } from "@/lib/useNow";

const UPCOMING = 10;

const berlinDate = (iso: string) =>
  new Intl.DateTimeFormat("en-CA", { timeZone: TIMEZONE, year: "numeric", month: "2-digit", day: "2-digit" }).format(
    new Date(iso),
  );

export function DeepDives({ generatedAt, popular }: { generatedAt: string; popular: PopularFixture[] }) {
  const now = useNow(generatedAt);
  const upcoming = useMemo(() => popular.filter((p) => isUpcoming(p.kickoffAt, now)).slice(0, UPCOMING), [popular, now]);
  const past = useMemo(() => popular.filter((p) => !isUpcoming(p.kickoffAt, now)), [popular, now]);

  return (
    <Section
      title="Deep dives"
      subtitle="The fixtures the most booking slips were built on: how every book priced them, form, and what punters backed"
    >
      <p className="mb-4 max-w-3xl text-sm leading-relaxed text-muted">
        Ranked by how many distinct slips name the fixture, so one hugely-copied slip cannot make its fixtures look
        widely backed. Upcoming and past are ranked separately: the most-slipped fixtures of all time are old ones.
      </p>
      <SubHeading>Upcoming · the {upcoming.length} most-slipped matches still to be played</SubHeading>
      {upcoming.length === 0 ? (
        <Empty>None.</Empty>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {upcoming.map((p, i) => (
            <Link key={p.eventId} href={`/fixture/${p.eventId}`} className="group">
              <Card className="flex h-full flex-col p-4 transition-all group-hover:-translate-y-0.5 group-hover:border-accent group-hover:shadow-md">
                <div className="flex items-center justify-between text-xs text-faint">
                  <span>#{i + 1}</span>
                  <span>{kickoff(p.kickoffAt)}</span>
                </div>
                <div className="mt-2 font-semibold leading-snug">{p.fixture}</div>
                <div className="mt-0.5 truncate text-xs text-muted">{p.tournament ?? "—"}</div>
                <div className="mt-auto flex items-end justify-between pt-4">
                  <div>
                    <div className="text-lg font-semibold tabular-nums">{int(p.slips)}</div>
                    <div className="text-xs text-faint">slips · {int(p.follows)} copies</div>
                  </div>
                  <span className="whitespace-nowrap text-sm font-medium text-accent">Deep dive →</span>
                </div>
              </Card>
            </Link>
          ))}
        </div>
      )}
      <PastTable past={past} />
    </Section>
  );
}

function PastTable({ past }: { past: PopularFixture[] }) {
  const router = useRouter();
  const competitions = useMemo(
    () => [...new Set(past.map((p) => p.tournament ?? "—"))].sort((a, b) => a.localeCompare(b)),
    [past],
  );
  const dates = useMemo(() => past.map((p) => berlinDate(p.kickoffAt)).sort(), [past]);
  const [text, setText] = useState("");
  const [leagues, setLeagues] = useState<string[]>([]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [minSlips, setMinSlips] = useState(0);
  const [rows, setRows] = useState(20);
  const [sort, setSort] = useState<{ key: "slips" | "follows" | "kickoffAt" | "fixture"; desc: boolean }>({
    key: "slips",
    desc: true,
  });

  const matching = useMemo(() => {
    const needle = text.trim().toLowerCase();
    const filtered = past.filter((p) => {
      const day = berlinDate(p.kickoffAt);
      return (
        (!needle || p.fixture.toLowerCase().includes(needle)) &&
        (!leagues.length || leagues.includes(p.tournament ?? "—")) &&
        (!from || day >= from) &&
        (!to || day <= to) &&
        p.slips >= minSlips
      );
    });
    const dir = sort.desc ? -1 : 1;
    return filtered.sort((a, b) => {
      const av = a[sort.key] ?? 0;
      const bv = b[sort.key] ?? 0;
      return (av < bv ? -1 : av > bv ? 1 : 0) * dir;
    });
  }, [past, text, leagues, from, to, minSlips, sort]);
  const shown = matching.slice(0, rows);

  const header = (key: typeof sort.key, label: string, right?: boolean) => (
    <Th right={right}>
      <button
        onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== "fixture" }))}
        className="inline-flex items-center gap-1 hover:text-text"
      >
        {label}
        <span className={clsx("text-[10px]", sort.key === key ? "text-accent" : "text-transparent")}>{sort.desc ? "▼" : "▲"}</span>
      </button>
    </Th>
  );

  return (
    <>
      <SubHeading note="Every played fixture punters built slips on. Filters run over all of them; select a row to open its deep dive.">
        Past · matches already played
      </SubHeading>
      {past.length === 0 ? (
        <Empty>None.</Empty>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
            <Field label="Fixture contains" className="col-span-2">
              <input
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="team name"
                className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
              />
            </Field>
            <Field label="Competition" className="col-span-2">
              <MultiSelect options={competitions} value={leagues} onChange={setLeagues} />
            </Field>
            <Field label="From">
              <input
                type="date"
                value={from}
                min={dates[0]}
                max={dates[dates.length - 1]}
                onChange={(e) => setFrom(e.target.value)}
                className="w-full rounded-lg border border-border bg-surface px-2 py-2 text-sm outline-none focus:border-accent"
              />
            </Field>
            <Field label="To">
              <input
                type="date"
                value={to}
                min={dates[0]}
                max={dates[dates.length - 1]}
                onChange={(e) => setTo(e.target.value)}
                className="w-full rounded-lg border border-border bg-surface px-2 py-2 text-sm outline-none focus:border-accent"
              />
            </Field>
          </div>
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <Field label="Min slips">
              <input
                type="number"
                min={0}
                value={minSlips}
                onChange={(e) => setMinSlips(Math.max(0, Number(e.target.value) || 0))}
                className="w-24 rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
              />
            </Field>
            <Field label="Rows">
              <select
                value={rows}
                onChange={(e) => setRows(Number(e.target.value))}
                className="rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
              >
                {[20, 50, 100, 250].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </Field>
            {(text || leagues.length || from || to || minSlips) ? (
              <button
                onClick={() => {
                  setText("");
                  setLeagues([]);
                  setFrom("");
                  setTo("");
                  setMinSlips(0);
                }}
                className="mb-0.5 rounded-lg px-3 py-2 text-sm font-medium text-accent hover:bg-accent-soft"
              >
                Clear filters
              </button>
            ) : null}
            <span className="mb-2 ml-auto text-sm text-muted">
              Showing {shown.length} of {int(matching.length)} matching fixtures
            </span>
          </div>
          <div className="mt-3">
            <Table maxHeight={560}>
              <thead>
                <tr>
                  {header("fixture", "Fixture")}
                  <Th>Competition</Th>
                  {header("kickoffAt", "Kick-off")}
                  {header("slips", "Slips", true)}
                  {header("follows", "Copies", true)}
                </tr>
              </thead>
              <tbody>
                {shown.map((p) => (
                  <tr
                    key={p.eventId}
                    onClick={() => router.push(`/fixture/${p.eventId}`)}
                    className="cursor-pointer hover:bg-surface-2"
                  >
                    <Td className="font-medium">
                      <Link href={`/fixture/${p.eventId}`} className="hover:text-accent" onClick={(e) => e.stopPropagation()}>
                        {p.fixture}
                      </Link>
                    </Td>
                    <Td className="text-muted">{p.tournament ?? "—"}</Td>
                    <Td className="whitespace-nowrap">
                      {shortDay(p.kickoffAt)}, {clock(p.kickoffAt)}
                    </Td>
                    <Td right>{int(p.slips)}</Td>
                    <Td right>{int(p.follows)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        </>
      )}
    </>
  );
}

function Field({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={className}>
      <div className="mb-1 text-xs font-medium text-muted">{label}</div>
      {children}
    </div>
  );
}

function MultiSelect({ options, value, onChange }: { options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const visible = options.filter((o) => o.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between rounded-lg border border-border bg-surface px-3 py-2 text-left text-sm"
      >
        <span className={clsx("truncate", !value.length && "text-faint")}>
          {value.length ? (value.length === 1 ? value[0] : `${value.length} competitions`) : "All competitions"}
        </span>
        <span className="text-faint">▾</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className="absolute z-30 mt-1 w-full min-w-64 rounded-lg border border-border bg-surface p-2 shadow-xl">
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search competitions"
              className="mb-2 w-full rounded-md border border-border bg-surface px-2 py-1.5 text-sm outline-none focus:border-accent"
            />
            <div className="max-h-64 overflow-auto">
              {visible.map((o) => (
                <label key={o} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-surface-2">
                  <input
                    type="checkbox"
                    checked={value.includes(o)}
                    onChange={() => onChange(value.includes(o) ? value.filter((v) => v !== o) : [...value, o])}
                  />
                  <span className="truncate">{o}</span>
                </label>
              ))}
            </div>
            {value.length > 0 && (
              <button onClick={() => onChange([])} className="mt-1 w-full rounded-md px-2 py-1.5 text-sm text-accent hover:bg-accent-soft">
                Clear
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
