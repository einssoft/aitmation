"""Smoke tests: the package imports and carries a version.

Real tests arrive with the state machine (session 3). This file exists so
that the test harness runs green from the very first commit.
"""

import aitmation_core


def test_package_imports() -> None:
    assert aitmation_core.__version__


def test_version_matches_semver_shape() -> None:
    parts = aitmation_core.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
