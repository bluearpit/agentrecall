from __future__ import annotations

from pathlib import Path

import pytest

from agentrecall.layout import Layout


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("AGENTRECALL_SKIP_UPDATE_CHECK", "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home_dir


@pytest.fixture
def layout(home: Path) -> Layout:
    return Layout.from_environ()
