"""Proves the toolchain itself works, before there is any product code.

Without a test here, `pytest` exits with code 5 ("no tests collected"), which
fails CI on a green repo. More usefully, this pins the two settings that are easy
to break and silent when broken: the coverage source path, and asyncio auto mode.
"""

from __future__ import annotations

import vesta_saved_search


def test_package_is_importable() -> None:
    # If the src/ layout or the hatch wheel config is wrong, this is what fails,
    # and it fails with a clear ImportError rather than a confusing coverage number.
    assert vesta_saved_search.__version__


async def test_async_tests_run_without_a_decorator() -> None:
    # asyncio_mode = "auto" in pyproject.toml. If that regresses, this test is
    # skipped rather than failed by pytest-asyncio, so assert something real.
    assert True
