"""Apply `snowflake/ddl.sql` to the warehouse.

The schema has changed several times already and will change again, so it is a
script rather than a paste into a worksheet: a clean clone reaches the same
warehouse state by running this, and the README can say so honestly.

`execute_string` handles the multi-statement file; splitting on semicolons by
hand is the classic way to break on a semicolon inside a comment or a string.
"""

from __future__ import annotations

from pathlib import Path

from arbibet_capstone.env import load as load_env
from arbibet_capstone.warehouse import connect

DDL = Path(__file__).with_name("ddl.sql")


load_env()


def main() -> None:
    with connect() as conn:
        for cursor in conn.execute_string(DDL.read_text()):
            if cursor.rowcount is not None and cursor.query:
                print(f"  {cursor.query.strip().splitlines()[0][:70]}")
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES IN ARBIBET_CAPSTONE.CORE")
            names = sorted(row[1] for row in cur.fetchall())
    print(f"\n{len(names)} tables in CORE: {', '.join(names)}")


if __name__ == "__main__":
    main()
