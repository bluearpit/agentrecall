from __future__ import annotations

from pathlib import Path

import pytest

from dotagents.layout import AgentName, Layout


def test_default_homes(home: Path, layout: Layout) -> None:
    assert layout.agents_home == home / ".agents"
    assert layout.claude_home == home / ".claude"
    assert layout.codex_home == home / ".codex"
    assert layout.cursor_home == home / ".cursor"
    assert layout.opencode_home == home / ".config" / "opencode"


def test_env_overrides(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = home / "custom-claude"
    codex = home / "custom-codex"
    xdg = home / "xdg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    layout = Layout.from_environ()
    assert layout.claude_home == claude
    assert layout.codex_home == codex
    assert layout.opencode_home == xdg / "opencode"
    assert layout.agent(AgentName.claude).reads_agents_skills_natively is False
    assert layout.agent(AgentName.cursor).reads_agents_skills_natively is True
    assert layout.agent(AgentName.codex).reads_agents_skills_natively is True
    assert layout.agent(AgentName.opencode).reads_agents_skills_natively is True
