"""Day-0 gate: does one bronze fixture resolve into a shared market id space?"""

import sys
from collections import defaultdict
from uuid import UUID

from arbibet_capstone.bronze import connect, latest_payloads
from arbibet_capstone.crosswalk.mappings import market_mappings
from arbibet_capstone.crosswalk.parsers import parse_bookmaker

BETRADAR = {"msport", "sportybet", "ilotbet"}
DIALECT = {"bet9ja", "livescorebet"}

event_id = UUID(sys.argv[1])
with connect() as conn:
    payloads = latest_payloads(conn, event_id)

print(f"bronze returned {len(payloads)} books: {sorted(payloads)}\n")

books_by_market: dict[str, set[str]] = defaultdict(set)
for book, body in payloads.items():
    if book not in BETRADAR | DIALECT:
        continue
    markets = parse_bookmaker(book, __import__("json").loads(body), market_mappings())
    print(f"  {book:<14} {len(markets):>4} markets")
    for m in markets:
        books_by_market[m.market_id].add(book)

shared = {
    mid: bks for mid, bks in books_by_market.items()
    if bks & BETRADAR and bks & DIALECT
}
print(f"\nmarkets shared between a betradar book and a dialect book: {len(shared)}")
for mid, bks in sorted(shared.items())[:10]:
    print(f"  marketId {mid:<12} {sorted(bks)}")

sys.exit(0 if shared else 1)
