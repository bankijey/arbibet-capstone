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

st.set_page_config(page_title="Arbibet", page_icon="::", layout="wide")

navigation = st.navigation(
    [
        st.Page(
            "views/overview.py",
            title="Market signals",
            url_path="overview",
            default=True,
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
