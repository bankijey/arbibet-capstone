"""Publish serving tables to Supabase. Filled in once the project exists."""

from __future__ import annotations

import os
from typing import Any


def configured() -> bool:
    return bool(os.environ.get("SUPABASE_DB_URL"))


def publish(warehouse: Any, run: Any) -> int:  # pragma: no cover - placeholder
    raise NotImplementedError("serving to Supabase is not built yet")
