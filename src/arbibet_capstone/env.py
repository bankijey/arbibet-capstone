"""Reading configuration from the environment.

Two jobs, both learned the hard way:

`load()` reads `.env` into the process. Nothing does this automatically — a
Python process sees only real environment variables — so a script that reads
`os.environ` directly works when you export variables by hand and fails the
moment you rely on the file. **Entry points call this; library modules never
do.** A library that reads a file on import is a library that behaves
differently depending on where it was imported from.

`require()` turns a missing variable into a sentence instead of a `KeyError`
with a variable name and no advice.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv


def load() -> None:
    """Load `.env` into the process, if there is one.

    Real environment variables win: `override=False` is the default and is the
    behaviour we want, because Airflow injects configuration that must beat a
    stale file mounted alongside it.
    """
    load_dotenv()


def require(name: str) -> str:
    """The value of `name`, or an error that says what to do about it."""
    try:
        return os.environ[name]
    except KeyError:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"and make sure the entry point calls arbibet_capstone.env.load()."
        ) from None
