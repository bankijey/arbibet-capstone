"""Publish one canonical snapshot per fixture to `market.ticks`.

One pass over a time window, then exit. Re-runnable, easy to reason about, and
the shape the Airflow task wants; a `while True` here would be a scheduler
nobody asked for.

Run:
    python producer/main.py [hours_back] [hours_ahead]
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from confluent_kafka import KafkaException, Producer

from arbibet_capstone import bronze, cursor, fixtures, snapshot
from arbibet_capstone.env import load as load_env
from arbibet_capstone.warehouse import connect as warehouse_connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("producer")


load_env()

# One ceiling, stated once, and it must match the topic's `max.message.bytes`
# and the consumers' `max.partition.fetch.bytes`: a producer allowed to send
# what the broker refuses, or the broker holding what a consumer cannot fetch,
# is the same failure moved somewhere quieter. 8 MiB is ~7x the largest raw
# snapshot seen and ~80x its compressed size.
MESSAGE_MAX_BYTES = 8 * 1024 * 1024

# Namespaces this job's positions in `pipeline_cursor`.
CURSOR_SCOPE = "producer"


def main(hours_back: float, hours_ahead: float) -> int:
    now = datetime.now(UTC)
    topic = os.environ["KAFKA_TOPIC"]
    producer = Producer(
        {
            "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"],
            # A snapshot is every book's every market for one fixture, and a
            # big match is big: Real Sociedad v Celta Vigo was 5 books, 1,624
            # markets, 1,150,521 bytes -- over librdkafka's 1,000,000-byte
            # default, which it enforces CLIENT-SIDE by raising from produce().
            # That one fixture killed the whole daily run. The JSON compresses
            # about 11x, so gzip takes the worst case to ~100 KB; the raised
            # ceiling is a second line of defence, mirrored on the topic and in
            # both consumers' fetch settings so no side is the odd one out.
            "compression.type": "gzip",
            "message.max.bytes": MESSAGE_MAX_BYTES,
        }
    )

    with fixtures.connect() as sources:
        window = fixtures.upcoming(
            sources,
            since=now - timedelta(hours=hours_back),
            until=now + timedelta(hours=hours_ahead),
        )

    # PRE-KICKOFF ONLY, and this is a correctness filter, not a tidy-up.
    #
    # Arbitrage and EV are claims about a bet you can still place. Once a match
    # kicks off the books suspend, freeze or violently re-price their markets,
    # and they do it at different moments -- so one book's frozen number sits
    # beside another's live one and the arithmetic reads as free money. Every
    # one of the 23 rows that failed `assert_arbitrage_is_physically_plausible`
    # was detected AFTER kickoff, none before; the worst was found 12 hours
    # after the whistle. Over/Under 2.5 priced 2.80 / 3.00 is not an
    # opportunity, it is two clocks disagreeing.
    #
    # The `hours_back` window stays: bronze needs a moment to have polled a
    # fixture, so the window is about DATA availability while this test is
    # about whether the bet exists. They are different questions.
    playable = [f for f in window if f.kickoff > now]
    log.info(
        "fixtures in window: %d (pre-kickoff: %d, already started: %d)",
        len(window), len(playable), len(window) - len(playable),
    )
    window = playable

    # Skip fixtures whose prices have not moved since the last run.
    #
    # Bronze is append-on-change, so its newest write_time for a fixture is
    # exactly "when this fixture last had news". If that is no newer than what
    # we published last time, re-parsing five books and re-publishing an
    # identical snapshot buys nothing: the consumers would recompute the same
    # signal_key and MERGE the same row over itself.
    #
    # Positions are read and written per fixture, and a fixture is only
    # advanced AFTER its message is queued -- so a crash mid-run leaves it
    # unadvanced and the next run picks it up again.
    fresh: dict[str, Any] = {}
    if window and not os.environ.get("PRODUCER_NO_CACHE"):
        with bronze.connect() as markets_conn:
            newest = bronze.latest_write_times(markets_conn, [f.event_id for f in window])
        try:
            with warehouse_connect() as wh:
                seen = cursor.read(wh, CURSOR_SCOPE)
        except Exception:
            # The cache is an optimisation. A warehouse that is unreachable
            # should make this run slow, not fail it.
            log.warning("cursor unavailable; analysing every fixture", exc_info=True)
            seen = {}
        unchanged = [
            f for f in window
            if f.event_id in newest and seen.get(str(f.event_id)) == newest[f.event_id]
        ]
        fresh = {str(f.event_id): newest[f.event_id] for f in window if f.event_id in newest}
        window = [f for f in window if f not in set(unchanged)]
        log.info("unchanged since last run: %d, analysing: %d", len(unchanged), len(window))

    published = skipped = failed = 0
    undelivered = 0

    def on_delivery(err: object, msg: object) -> None:
        # produce() only hands a message to librdkafka's queue; whether it
        # ever reached the broker is reported HERE, asynchronously. Before this
        # callback the producer could run to completion, log published=81 and
        # exit 0 while every message sat in the queue being refused -- which
        # is exactly what happened when the container was advertised
        # `localhost:9092` and dutifully connected to itself for five minutes.
        # A message that never landed is a failure, and must count as one.
        nonlocal undelivered
        if err is not None:
            undelivered += 1
            log.error("delivery failed: %s", err)

    with bronze.connect() as markets:
        for fixture in window:
            try:
                snap = snapshot.fetch(markets, fixture)
            except Exception:
                # Fixture-level isolation, and the ONLY place it belongs. A
                # payload that will not parse is a real defect -- a book
                # changed shape, or the crosswalk is wrong -- so it is logged
                # with its traceback and its fixture, never folded into an
                # empty result that reads as "no markets".
                log.error("fixture %s failed to build", fixture.event_id, exc_info=True)
                failed += 1
                continue

            if not snap.books:
                # Normal: bronze has not polled this fixture yet, or no book
                # with a parser priced it.
                skipped += 1
                continue

            # Keyed by event_id so every snapshot for a fixture lands on one
            # partition and stays ordered relative to its own history.
            value = json.dumps(snapshot.to_wire(snap)).encode()
            try:
                producer.produce(
                    topic, key=str(fixture.event_id), value=value, on_delivery=on_delivery
                )
            except KafkaException:
                # Fixture-level isolation for the wire, matching the one for
                # the parse above. produce() raises synchronously for a
                # message over message.max.bytes, and before this guard one
                # oversized fixture took the whole daily run down with it.
                # Logged with the size and the fixture so it is a visible
                # skip, never a silent one -- and counted as a failure, so
                # the exit code still says something went wrong.
                log.error(
                    "fixture %s (%s v %s) not published: %d bytes",
                    fixture.event_id, fixture.home_team, fixture.away_team, len(value),
                    exc_info=True,
                )
                failed += 1
                continue
            published += 1

    # flush() blocks until every queued message is delivered or gives up
    # (message.timeout.ms), and returns how many are STILL outstanding. Both
    # that remainder and the delivery-report failures are messages that never
    # reached the topic: they come off `published` and go on `failed`, so the
    # log line and the exit code describe what actually landed.
    remaining = producer.flush()
    lost = undelivered + remaining
    if lost:
        log.error("%d message(s) never reached %s", lost, topic)
        published -= lost
        failed += lost

    # Advance positions only after a CLEAN run. A partially-delivered run must
    # be repeatable, and the cheapest way to guarantee that is to leave the
    # positions where they were and let the next run redo the lot.
    if fresh and not (failed or lost):
        try:
            with warehouse_connect() as wh:
                cursor.write(wh, CURSOR_SCOPE, fresh)
            log.info("cursor advanced for %d fixtures", len(fresh))
        except Exception:
            log.warning("cursor write failed; next run re-analyses", exc_info=True)

    log.info("published=%d skipped=%d failed=%d", published, skipped, failed)
    return failed


if __name__ == "__main__":
    back = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    ahead = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
    raise SystemExit(1 if main(back, ahead) else 0)
