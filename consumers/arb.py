"""Consume `market.ticks`, detect arbitrage, write `fact_arbitrage_signal`.

Runs until the topic drains -- `IDLE_EXIT_SECONDS` with no message -- then
exits. A batch shape, so it is re-runnable and an Airflow task can wait on it.

Offsets commit only after the write succeeds, which makes this at-least-once:
a crash between write and commit replays the message, and the MERGE on
`signal_key` absorbs the repeat.
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
from arbibet_capstone.signals import MAX_LEG_SPREAD_SECONDS, arbitrage_rows
from arbibet_capstone.snapshot import from_wire
from arbibet_capstone.warehouse import connect, merge

load_env()

# The vendored engine concatenates empty frames on nearly every message. The
# warning is about a future pandas behaviour change; at one line per message it
# drowns the log this consumer is meant to be read from.
warnings.filterwarnings(
    "ignore", category=FutureWarning, module="arbibet_capstone.crosswalk"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# The vendored engine logs a timing line per stage per message. Useful when
# it was a foreground script, noise at one message a second.
logging.getLogger("arbibet_capstone.crosswalk.arbitrage").setLevel(logging.WARNING)
log = logging.getLogger("consumer.arb")

TABLE = "fact_arbitrage_signal"
KEY = ["signal_key"]
JSON_COLUMNS = {"legs"}


def main() -> int:
    threshold = float(os.environ.get("ARB_RECORD_THRESHOLD", "0.98"))
    max_spread = int(os.environ.get("ARB_MAX_LEG_SPREAD_SECONDS", MAX_LEG_SPREAD_SECONDS))
    idle_exit = float(os.environ.get("IDLE_EXIT_SECONDS", "10"))
    # Bounded separately from the idle clock, and much longer, because joining
    # a consumer group is not the same event as running out of messages.
    join_wait = float(os.environ.get("JOIN_WAIT_SECONDS", "90"))
    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"],
            # A group of its own. Kafka splits partitions across a group's
            # members, so sharing one with the EV consumer would hand one of
            # them every message and the other none, silently.
            "group.id": os.environ["KAFKA_GROUP_ARB"],
            "auto.offset.reset": "earliest",
            # Must match the producer's message.max.bytes and the topic's
            # max.message.bytes (8 MiB): a big fixture snapshot the broker
            # accepted but the consumer cannot fetch is a silent stall, not
            # an error. See producer/main.py for the fixture that set the bar.
            "max.partition.fetch.bytes": 8 * 1024 * 1024,
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([os.environ["KAFKA_TOPIC"]])
    log.info("threshold=%.3f (below 1.0 records near-arbitrage)", threshold)

    seen = written = failed = 0
    try:
        with connect() as warehouse:
            # Start the idle clock AFTER connecting. Snowflake's handshake
            # takes several seconds, and starting it earlier can exhaust the
            # window before the first poll -- a consumer that reports zero
            # messages against a full topic.
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
                            "topic, a consumer that never joined", join_wait
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
                    # A tombstone. This producer never writes one, but a
                    # null-valued record is legal Kafka and would otherwise
                    # crash the loop on a TypeError far from the cause.
                    log.warning("null-valued message at offset %s", message.offset())
                    continue

                seen += 1
                try:
                    snapshot = from_wire(json.loads(payload))
                    rows = arbitrage_rows(
                        snapshot, threshold=threshold, max_leg_spread_seconds=max_spread
                    )
                    # detected_at is market time now; this is ours. Kept so the
                    # gap between a price existing and us noticing it stays
                    # measurable.
                    consumed_at = datetime.now(UTC)
                    rows = [{**r, "consumed_at": consumed_at} for r in rows]
                    if rows:
                        written += merge(
                            warehouse,
                            table=TABLE,
                            rows=rows,
                            key=KEY,
                            json_columns=JSON_COLUMNS,
                        )
                except Exception:
                    # Message-level isolation: one bad snapshot must not cost
                    # the run. Logged with its traceback, never swallowed.
                    log.error(
                        "message at offset %s failed", message.offset(), exc_info=True
                    )
                    failed += 1
                    continue
                consumer.commit(message=message)
    finally:
        consumer.close()

    log.info("messages=%d rows_written=%d failed=%d", seen, written, failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
