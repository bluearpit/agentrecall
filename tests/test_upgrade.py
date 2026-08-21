from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.layout import Layout
from agentrecall.upgrade import (
    cached_latest,
    is_newer,
    notice_if_outdated,
    parse_version,
    upgrade_plan,
)

runner = CliRunner()


def test_parse_and_compare_versions() -> None:
    assert parse_version("v0.2.0") == (0, 2, 0)
    assert is_newer("0.2.0", "0.1.0")
    assert not is_newer("0.1.0", "0.2.0")
    assert not is_newer("0.2.0", "0.2.0")
    assert is_newer("0.2", "0.1.9")


def test_cached_latest_reuses_fresh_cache(layout: Layout) -> None:
    calls = {"n": 0}

    def fetch() -> str | None:
        calls["n"] += 1
        return "9.9.9"

    first = cached_latest(layout, fetch=fetch)
    second = cached_latest(layout, fetch=fetch)
    assert first == "9.9.9"
    assert second == "9.9.9"
    assert calls["n"] == 1


def test_cached_latest_refetches_after_interval(layout: Layout) -> None:
    values = iter(["1.0.0", "2.0.0"])

    def fetch() -> str | None:
        return next(values)

    now = datetime(2026, 8, 21, tzinfo=UTC)
    assert cached_latest(layout, now=now, fetch=fetch) == "1.0.0"
    later = now + timedelta(hours=25)
    assert cached_latest(layout, now=later, fetch=fetch) == "2.0.0"


def test_notice_if_outdated_asks_before_upgrade(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTRECALL_SKIP_UPDATE_CHECK", raising=False)
    notice = notice_if_outdated(layout, current="0.1.0", fetch=lambda: "0.2.0")
    assert notice is not None
    assert "0.2.0" in notice
    assert "Ask before upgrading" in notice
    assert notice_if_outdated(layout, current="0.2.0", fetch=lambda: "0.2.0") is None


def test_upgrade_plan_and_cli_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    lines, failed = upgrade_plan(current="0.1.0", latest="0.2.0")
    assert failed is False
    assert any("0.1.0 -> 0.2.0" in line for line in lines)

    monkeypatch.setattr("agentrecall.cli.fetch_latest_version", lambda: "9.9.9")
    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert "would upgrade" in result.stdout
    assert "git+https://github.com/bluearpit/agentrecall.git@v9.9.9" in result.stdout


def test_upgrade_apply_runs_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentrecall.cli.fetch_latest_version", lambda: "9.9.9")
    monkeypatch.setattr(
        "agentrecall.cli.apply_upgrade", lambda latest: f"installed agentrecall {latest}"
    )
    result = runner.invoke(app, ["upgrade", "--apply"])
    assert result.exit_code == 0
    assert "installed agentrecall 9.9.9" in result.stdout
