from __future__ import annotations

from pathlib import Path


def test_home_is_isolated_under_tmp(home: Path, tmp_path: Path) -> None:
    assert Path.home() == home
    assert home == tmp_path / "home"
    assert home.is_dir()
