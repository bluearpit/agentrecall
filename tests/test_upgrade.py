from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import URLError

import pytest
from typer.testing import CliRunner

from agentrecall.cli import app
from agentrecall.layout import Layout
from agentrecall.upgrade import (
    PYPI_URL,
    cached_latest,
    fetch_latest_version,
    install_spec,
    is_newer,
    notice_if_outdated,
    parse_version,
    update_check_file,
    upgrade_plan,
)

runner = CliRunner()


def test_parse_and_compare_versions() -> None:
    assert parse_version("v0.2.0") == (0, 2, 0)
    assert is_newer("0.2.0", "0.1.0")
    assert not is_newer("0.1.0", "0.2.0")
    assert not is_newer("0.2.0", "0.2.0")
    assert is_newer("0.2", "0.1.9")
    assert install_spec("v9.9.9") == "agentrecall-cli==9.9.9"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_fetch_latest_version_reads_pypi_info(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        seen["url"] = request.full_url  # type: ignore[attr-defined]
        body = json.dumps({"info": {"version": "0.9.1"}, "releases": {}})
        return _FakeResponse(body.encode("utf-8"))

    monkeypatch.setattr("agentrecall.upgrade.urlopen", fake_urlopen)
    assert fetch_latest_version() == "0.9.1"
    assert seen["url"] == PYPI_URL
    assert PYPI_URL == "https://pypi.org/pypi/agentrecall-cli/json"


def test_fetch_latest_version_returns_none_on_bad_payload_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_info(request: object, timeout: float) -> _FakeResponse:
        return _FakeResponse(b'{"releases": {}}')

    monkeypatch.setattr("agentrecall.upgrade.urlopen", no_info)
    assert fetch_latest_version() is None

    def offline(request: object, timeout: float) -> _FakeResponse:
        raise URLError("offline")

    monkeypatch.setattr("agentrecall.upgrade.urlopen", offline)
    assert fetch_latest_version() is None


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


def test_cached_latest_remembers_failed_check(layout: Layout) -> None:
    calls = {"n": 0}

    def fetch() -> str | None:
        calls["n"] += 1
        return None

    now = datetime(2026, 8, 21, tzinfo=UTC)
    assert cached_latest(layout, now=now, fetch=fetch) is None
    assert cached_latest(layout, now=now + timedelta(hours=1), fetch=fetch) is None
    assert calls["n"] == 1
    assert update_check_file(layout).is_file()
    assert cached_latest(layout, now=now + timedelta(hours=25), fetch=fetch) is None
    assert calls["n"] == 2


def test_cached_latest_keeps_last_known_version_when_refresh_fails(layout: Layout) -> None:
    values = iter(["1.0.0", None, "2.0.0"])

    def fetch() -> str | None:
        return next(values)

    now = datetime(2026, 8, 21, tzinfo=UTC)
    assert cached_latest(layout, now=now, fetch=fetch) == "1.0.0"
    stale = now + timedelta(hours=25)
    assert cached_latest(layout, now=stale, fetch=fetch) == "1.0.0"
    assert cached_latest(layout, now=stale + timedelta(hours=1), fetch=fetch) == "1.0.0"
    assert cached_latest(layout, now=stale + timedelta(hours=25), fetch=fetch) == "2.0.0"


def test_cached_latest_ignores_corrupt_cache(layout: Layout) -> None:
    path = update_check_file(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert cached_latest(layout, fetch=lambda: "3.0.0") == "3.0.0"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["latest"] == "3.0.0"


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
    assert "agentrecall-cli==9.9.9" in result.stdout


def test_upgrade_apply_runs_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentrecall.cli.fetch_latest_version", lambda: "9.9.9")
    monkeypatch.setattr(
        "agentrecall.cli.apply_upgrade", lambda latest: f"installed agentrecall {latest}"
    )
    result = runner.invoke(app, ["upgrade", "--apply"])
    assert result.exit_code == 0
    assert "installed agentrecall 9.9.9" in result.stdout
