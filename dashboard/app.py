"""The capstone dashboard: entry point and navigation.

Run with:
    python -m streamlit run dashboard/app.py

`st.navigation` rather than a `pages/` directory. The directory form takes each
page's sidebar label from its FILENAME, which gave a sidebar reading "app" and
"fixture"; this declares the labels. `url_path` is pinned so the deep-dive
links stay `/fixture?event_id=...` -- they are shareable, so the path is part
of the contract and must not drift with a rename.

Nothing here imports `arbibet_capstone`. The dashboard reads the serving
database the pipeline publishes to (dashboard/common.py), not the warehouse,
and Streamlit Community Cloud installs from `requirements.txt`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# The dashboard is an entry point, and entry points load `.env` -- the rule the
# rest of the project follows through `arbibet_capstone.env.load()`, which this
# file cannot use because it deliberately imports no project code. Deployed to
# Streamlit Cloud there is no .env and this is a silent no-op; credentials come
# from st.secrets there.
load_dotenv()

# `streamlit run dashboard/app.py` puts `dashboard/` on the path, not the repo
# root, so `from dashboard.common import ...` inside the views would fail.
# Adding the root keeps the views importable as a package either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _reload_changed_modules() -> None:
    """Re-import any `dashboard.*` module whose file changed since it was imported.

    Streamlit Cloud deploys a push by re-running the page scripts in the SAME
    process. This file and the views are read fresh every run, but shared
    modules (`dashboard.common`, ...) stay in `sys.modules` as they were before
    the deploy -- so a view importing a name added in that push fails with an
    ImportError until someone reboots the app. It did: `fair_link`.

    The registry lives on `sys`, the one place that survives this script being
    re-executed.
    """
    import importlib

    registry: dict[str, float] = sys.__dict__.setdefault("_arbibet_module_mtimes", {})
    # Importers after what they import from.
    order = ["dashboard.common", "dashboard.series", "dashboard.arbitrage", "dashboard.backtest"]
    names = sorted(
        (n for n in list(sys.modules) if n.startswith("dashboard.") and ".views." not in n),
        key=lambda n: (order.index(n) if n in order else len(order), n),
    )
    for name in names:
        module = sys.modules.get(name)
        path = getattr(module, "__file__", None)
        if module is None or not path:
            continue
        try:
            modified = Path(path).stat().st_mtime
        except OSError:
            continue
        known = registry.get(name)
        # First sight of an already-imported module: it may predate a deploy
        # (that is the incident this exists for), so reload it once.
        if known is None or modified > known:
            if known is not None or name in _PREDEPLOY:
                importlib.reload(module)
            registry[name] = modified


# Modules that may be stale the first time this code runs on an already-running app.
_PREDEPLOY = {"dashboard.common", "dashboard.series", "dashboard.charts", "dashboard.backtest"}
_reload_changed_modules()

st.set_page_config(page_title="Arbibet", page_icon="::", layout="wide")

navigation = st.navigation(
    [
        # First: the proof. What placing every signal would have done, and how
        # copied slips fared -- the case for the signals before the signals.
        st.Page(
            "views/record.py",
            title="Track record",
            url_path="record",
            default=True,
        ),
        st.Page(
            "views/overview.py",
            title="Market signals",
            url_path="overview",
        ),
        st.Page(
            "views/slips.py",
            title="Betting slips",
            url_path="slips",
        ),
        st.Page(
            "views/health.py",
            title="Pipeline health",
            url_path="health",
        ),
        # Reached from a deep-dive card on the slips page, never from the
        # sidebar: a page that needs a fixture chosen first has no business in
        # the navigation. Hidden, but still routable, so `/fixture?event_id=`
        # links keep working.
        st.Page(
            "views/fixture.py",
            title="Fixture deep dive",
            url_path="fixture",
            visibility="hidden",
        ),
    ]
)

navigation.run()
