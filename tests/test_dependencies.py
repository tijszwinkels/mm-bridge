"""Guards against dependency-resolution regressions that break a fresh install.

`mm_client` imports ``Driver`` from ``mattermostautodriver``. That symbol was
dropped in 11.8.x (renamed to ``TypedDriver``): 11.7.2 still exports ``Driver``,
11.8.2 does not. Our ``uv.lock`` pins a working version, but ``uv tool install``
resolves *fresh* from ``pyproject.toml`` and ignores the lock — so without an
upper bound a stranger installing the CLI gets 11.9.0 and a dead
``mm-bridge --help`` (ImportError on ``Driver``).

These tests fail loudly if the cap is ever removed or loosened past the point
where ``Driver`` disappears.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# First mattermostautodriver release that dropped the ``Driver`` symbol, and the
# last release that still exports it. Verified against PyPI wheels.
_FIRST_BROKEN = Version("11.8.0")
_LAST_GOOD = Version("11.7.2")


def _mattermostautodriver_requirement() -> Requirement:
    data = tomllib.loads(_PYPROJECT.read_text())
    for raw in data["project"]["dependencies"]:
        req = Requirement(raw)
        if req.name == "mattermostautodriver":
            return req
    raise AssertionError("mattermostautodriver missing from project dependencies")


def test_mattermostautodriver_is_capped_below_the_driver_removal() -> None:
    req = _mattermostautodriver_requirement()
    assert not req.specifier.contains(_FIRST_BROKEN, prereleases=True), (
        f"pyproject.toml allows mattermostautodriver {_FIRST_BROKEN}, which "
        "dropped the `Driver` symbol that mm_client imports — cap it below 11.8"
    )
    assert req.specifier.contains(_LAST_GOOD, prereleases=True), (
        f"cap is too tight: it excludes {_LAST_GOOD}, the last release that "
        "still exports `Driver`"
    )


def test_driver_symbol_is_importable() -> None:
    """The resolved environment actually exposes the symbol mm_client needs."""
    from mattermostautodriver import Driver

    assert isinstance(Driver, type)
