"""Consume `market.ticks`, screen for positive EV, write `fact_ev_signal`.

The sibling of `arb.py` and deliberately its own process: a different question,
a different grain, and its own consumer group, so a failure or a slow write in
one never starves the other.
"""

from __future__ import annotations

import json
import logging
import os
import time
import warnings
from datetime import UTC, datetime

from confluent_kafka import Consumer

from arbibet_capstone.env import load as load_env
from arbibet_capstone.signals import MAX_PROBABILITY_SPREAD_SECONDS, ev_rows
from arbibet_capstone.snapshot import from_wire
from arbibet_capstone.warehouse import bookmaker_ids, connect, merge

load_env()

warnings.filterwarnings("ignore", category=FutureWarning, module="arbibet_capstone.crosswalk")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("arbibet_capstone.crosswalk.arbitrage").setLevel(logging.WARNING)
log = logging.getLogger("consumer.ev")

TABLE = "fact_ev_signal"
KEY = ["signal_key"]


def main() -> int:
    min_ev = float(os.environ.get("EV_MIN", "0.01"))
    min_p = float(os.environ.get("EV_MIN_PROBABILITY", "0.5"))
    idle_exit = float(os.environ.get("IDLE_EXIT_SECONDS", "10"))
    max_p_spread = int(
        os.environ.get("EV_MAX_PROBABILITY_SPREAD_SECONDS", MAX_PROBABILITY_SPREAD_SECONDS)
    )
    # Bounded separately from the idle clock, and much longer, because joining
    # a consumer group is not the same event as running out of messages.
    join_wait = float(os.environ.get("JOIN_WAIT_SECONDS", "90"))

    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"],
            # Its own group. Sharing one with the arb consumer would hand every
            # message to one of them and none to the other, silently.
            "group.id": os.environ["KAFKA_GROUP_EV"],
            "auto.offset.reset": "earliest",
            # Must match the producer's message.max.bytes and the topic's
            # max.message.bytes (8 MiB). See consumers/arb.py.
            "max.partition.fetch.bytes": 8 * 1024 * 1024,
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([os.environ["KAFKA_TOPIC"]])
    log.info("min_ev=%.3f min_probability=%.2f", min_ev, min_p)

    seen = written = failed = 0
    try:
        with connect() as warehouse:
            # Resolved once: dim_bookmaker is the single definition of these
            # ids, and re-reading it per message would be a query per message
            # for five rows that never change during a run.
            books = bookmaker_ids(warehouse)
            # The idle clock does not start until Kafka has actually GIVEN
            # this consumer something to read.
            #
            # Starting it after the Snowflake handshake was not enough. A
            # group whose previous member has left is rebalanced by the
            # coordinator, and that can take most of `session.timeout.ms`
            # (45s by default) -- far longer than the ten-second idle window.
            # A run at 18:02 reported `messages=0 rows_written=0` against a
            # topic holding 934 messages; the same consumer, rerun minutes
            # later, read all of them and wrote 138 rows. Exit code 0 both
            # times. On a half-hourly schedule that is a green task that
            # silently does nothing, which is the exact shape of every other
            # bug in this project: a plausible number, not an error.
            joined_by = time.monotonic() + join_wait
            last_message = time.monotonic()
            while True:
                if not consumer.assignment():
                    if time.monotonic() > joined_by:
                        log.error(
                            "no partition assignment after %.0fs -- not an empty "
                            "topic, a consumer that never joined",
                            join_wait,
                        )
                        return 1
                    last_message = time.monotonic()
                elif time.monotonic() - last_message >= idle_exit:
                    break
                message = consumer.poll(1.0)
                if message is None:
                    continue
                if message.error():
                    log.error("kafka: %s", message.error())
                    continue

                last_message = time.monotonic()
                payload = message.value()
                if payload is None:
                    log.warning("null-valued message at offset %s", message.offset())
                    continue

                seen += 1
                try:
                    snapshot = from_wire(json.loads(payload))
                    rows = []
                    consumed_at = datetime.now(UTC)
                    for row in ev_rows(
                        snapshot,
                        min_ev=min_ev,
                        min_probability=min_p,
                        max_probability_spread_seconds=max_p_spread,
                    ):
                        book = row.pop("bookmaker")
                        if book not in books:
                            # A book priced it but dim_bookmaker does not know
                            # it. Skipping silently would drop real signal, so
                            # say which book and move on.
                            log.warning("no dim_bookmaker row for %s", book)
                            continue
                        rows.append(
                            {"bookmaker_id": books[book], **row, "consumed_at": consumed_at}
                        )
                    if rows:
                        written += merge(warehouse, table=TABLE, rows=rows, key=KEY)
                except Exception:
                    log.error("message at offset %s failed", message.offset(), exc_info=True)
                    failed += 1
                    continue
                consumer.commit(message=message)
    finally:
        consumer.close()

    log.info("messages=%d rows_written=%d failed=%d", seen, written, failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
