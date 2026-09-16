import type { Metadata } from "next";
import Link from "next/link";

import { MarketExplorer } from "@/components/fixture/MarketExplorer";
import { NoSnapshot } from "@/components/NoSnapshot";
import { Punters, SettledMarkets, TeamForm } from "@/components/fixture/Record";
import { Callout, Card, Empty, Prose } from "@/components/ui";
import { berlinDay, findDive } from "@/lib/data";
import { fullDate, kickoff } from "@/lib/format";

export const revalidate = 3600;

// Rendered on first visit and cached; nothing is prerendered at build.
export async function generateStaticParams() {
  return [];
}

export async function generateMetadata({ params }: PageProps<"/fixture/[id]">): Promise<Metadata> {
  const { id } = await params;
  const { dive, live } = await findDive(id);
  const name = dive ? `${dive.home} v ${dive.away}` : live?.slips.popular.find((p) => p.eventId === id)?.fixture;
  return { title: name ?? "Fixture" };
}

export default async function FixturePage({ params }: PageProps<"/fixture/[id]">) {
  const { id } = await params;
  const { dive, live, archived } = await findDive(id);
  if (!live) return <NoSnapshot />;
  const listed = live.slips.popular.find((p) => p.eventId === id);

  const back = (
    <Link href="/slips" className="text-sm font-medium text-accent hover:underline">
      ← Betting slips
    </Link>
  );

  if (!dive) {
    return (
      <>
        {back}
        <h1 className="mt-3 text-2xl font-semibold tracking-tight">{listed?.fixture ?? "Fixture not found"}</h1>
        <div className="mt-6">
          <Empty>
            {listed
              ? archived && !live.archivedDays.includes(berlinDay(listed.kickoffAt))
                ? `This match kicked off ${kickoff(listed.kickoffAt)}. Its deep dive is published once every slipped match that day is three hours old.`
                : "No deep dive was published for this fixture."
              : "No fixture with this id in the current snapshot. Fixtures roll out of the window after a few weeks."}
          </Empty>
        </div>
      </>
    );
  }

  return (
    <>
      {back}
      <div className="mt-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            {dive.home} <span className="text-faint">v</span> {dive.away}
          </h1>
          <p className="mt-1 text-muted">
            {dive.tournament ?? "Competition unknown"} · kick-off {fullDate(dive.kickoffAt)}
          </p>
        </div>
        {listed && (
          <div className="text-right text-sm text-muted">
            <b className="text-text">{listed.slips.toLocaleString("en-GB")}</b> slips ·{" "}
            {(listed.follows ?? 0).toLocaleString("en-GB")} copies
          </div>
        )}
      </div>

      {!dive.hasTeams && (
        <div className="mt-4">
          <Callout tone="warn">
            This fixture never resolved to API-Football team ids, so no history is available for it. The market section
            still works. About a third of fixtures are in this state: priceable, with no record behind them.
          </Callout>
        </div>
      )}

      <div className={`mt-5 grid gap-4 ${dive.result ? "lg:grid-cols-2" : ""}`}>
        {dive.brief && (
          <Card className="p-5">
            <div className="mb-2 text-xs font-medium uppercase tracking-wide text-faint">Before kick-off</div>
            <Prose text={dive.brief.summary} />
            <p className="mt-3 text-xs leading-relaxed text-faint">
              Written by {dive.brief.model} from {dive.brief.markets} markets and {dive.brief.sides} sides&apos; match
              history in this warehouse, and from nothing else: no league position, no injuries, no past meetings.{" "}
              {kickoff(dive.brief.generatedAt)}.
            </p>
          </Card>
        )}
        {dive.result && (
          <Card className="p-5">
            <div className="mb-2 text-xs font-medium uppercase tracking-wide text-faint">After full time</div>
            <Prose text={dive.result.summary} />
            <p className="mt-3 text-xs text-faint">
              Written once by {dive.result.model}, {kickoff(dive.result.generatedAt)}.
            </p>
          </Card>
        )}
      </div>

      <h2 className="mt-10 text-lg font-semibold tracking-tight">How the books priced it</h2>
      {dive.markets.length ? (
        <MarketExplorer markets={dive.markets} kickoffAt={dive.kickoffAt} />
      ) : (
        <div className="mt-3">
          <Empty>No price history for this fixture. Prices are extracted only for fixtures that produced a signal or were slipped.</Empty>
        </div>
      )}

      {dive.hasTeams && (
        <>
          <h2 className="mt-10 text-lg font-semibold tracking-tight">What both sides had been doing</h2>
          <p className="mt-1 max-w-3xl text-sm text-muted">
            The last 10 matches each side played before this kick-off, from API-Football. Later results are excluded
            deliberately: judging how a fixture was priced using what happened afterwards is the most inviting mistake
            on this page.
          </p>
          {dive.teams.map((t) => (
            <TeamForm key={t.name + t.role} team={t} />
          ))}
          <h2 className="mt-10 text-lg font-semibold tracking-tight">How these markets have actually settled</h2>
          <p className="mt-1 max-w-3xl text-sm text-muted">
            The same markets the books price, resolved against real results for both sides over the same window. This is
            what the slip verdicts are built on; it is recent form and knows nothing about who the opponent was.
          </p>
          <SettledMarkets rows={dive.settled} />
        </>
      )}

      <h2 className="mt-10 text-lg font-semibold tracking-tight">What punters actually backed here</h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">
        Every booking slip that names this fixture, rolled up by pick: how many slips chose it, how many times those
        slips were copied, and at what price.
      </p>
      <Punters punters={dive.punters} />
    </>
  );
}
