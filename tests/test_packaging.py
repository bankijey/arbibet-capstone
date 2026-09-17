"""The two dependency lists must agree.

The image's pipeline venv is built from `airflow/requirements-pipeline.txt`,
not from `pyproject.toml`, because it also needs pyspark and dbt which the
package itself does not. That makes it a SECOND list, and a second list drifts:
`httpx` and `python-dotenv` were missing from it, and the first anyone knew was
`ModuleNotFoundError: No module named 'httpx'` three minutes into a DAG run,
in a container, with the traceback buried in an Airflow task log.

This turns that into a failing test on a laptop.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _names(requirements: list[str]) -> set[str]:
    """Distribution names, without version pins or extras."""
    return {
        re.split(r"[<>=!\[;]", line)[0].strip().lower()
        for line in requirements
        if line.strip() and not line.strip().startswith("#")
    }


def test_the_container_venv_carries_every_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = _names(project["project"]["dependencies"])
    installed = _names(
        (ROOT / "airflow" / "requirements-pipeline.txt").read_text(encoding="utf-8").splitlines()
    )

    missing = declared - installed
    assert not missing, (
        f"airflow/requirements-pipeline.txt is missing {sorted(missing)}. "
        "Every runtime dependency has to be in the image, or the task that "
        "needs it fails inside a container rather than here."
    )
