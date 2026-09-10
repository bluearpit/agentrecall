"""Check PyPI for a newer agentrecall-cli release. Never install without --apply."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from agentrecall import __version__
from agentrecall.layout import Layout

SKIP_ENV = "AGENTRECALL_SKIP_UPDATE_CHECK"
CHECK_INTERVAL = timedelta(hours=24)
PACKAGE = "agentrecall-cli"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"


def parse_version(raw: str) -> tuple[int, ...]:
    text = raw.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    parts: list[int] = []
    for piece in text.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    if not parts:
        raise ValueError(f"invalid version {raw!r}")
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    left = parse_version(latest)
    right = parse_version(current)
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right


def fetch_latest_version(url: str = PYPI_URL) -> str | None:
    """Return the newest version PyPI can install, or None when the check fails."""
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "agentrecall",
        },
    )
    try:
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    try:
        parse_version(version)
    except ValueError:
        return None
    return version.strip().lstrip("vV")


def update_check_file(layout: Layout) -> Path:
    """Cache lives under history/, the one extra root sandboxed agents are granted."""
    return layout.history_dir / "update-check.json"


def legacy_update_check_file(layout: Layout) -> Path:
    return layout.agents_home / "update-check.json"


def _cache_dir_writable(path: Path) -> bool:
    """Best-effort hint. Some sandboxes report writable and then deny the write."""
    for candidate in [path.parent, *path.parent.parents]:
        if candidate.exists():
            return os.access(candidate, os.W_OK)
    return False


def _read_update_check(path: Path) -> tuple[str | None, datetime | None]:
    """Return (latest, checked_at) from the cache file; missing or bad fields are None."""
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    cached = payload.get("latest")
    latest = cached if isinstance(cached, str) and cached.strip() else None
    checked_raw = payload.get("checked_at")
    if not isinstance(checked_raw, str):
        return latest, None
    try:
        checked = datetime.fromisoformat(checked_raw)
    except ValueError:
        return latest, None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return latest, checked


def _write_update_check(path: Path, *, latest: str | None, checked_at: datetime) -> bool:
    """Write the cache. Never raise: a blocked cache must not stop the command."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"latest": latest, "checked_at": checked_at.isoformat()}),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def _remove_legacy_cache(layout: Layout) -> None:
    try:
        legacy_update_check_file(layout).unlink(missing_ok=True)
    except OSError:
        pass


def cached_latest(
    layout: Layout,
    *,
    now: datetime | None = None,
    fetch: Callable[[], str | None] | None = None,
) -> str | None:
    """Return the newest known version, fetching at most once per CHECK_INTERVAL.

    A failed fetch still counts as a check, so an offline machine does not retry
    on every command. The last known version is kept until a fetch succeeds.
    When the cache cannot be written (read-only home, strict sandbox), skip the
    network call too and serve whatever an earlier run recorded.
    """
    moment = datetime.now(tz=UTC) if now is None else now
    path = update_check_file(layout)
    cached, checked = _read_update_check(path)
    if checked is not None and moment - checked < CHECK_INTERVAL:
        return cached
    if not _cache_dir_writable(path):
        return cached
    getter = fetch if fetch is not None else fetch_latest_version
    latest = getter()
    if latest is None:
        latest = cached
    if _write_update_check(path, latest=latest, checked_at=moment):
        _remove_legacy_cache(layout)
    return latest


def install_spec(latest: str) -> str:
    version = latest.strip().lstrip("vV")
    return f"{PACKAGE}=={version}"


def upgrade_argv(latest: str) -> tuple[str, ...]:
    return ("uv", "tool", "install", "--force", install_spec(latest))


def outdated_notice(current: str, latest: str) -> str:
    return (
        f"agentrecall {latest} is available (you have {current}). "
        "Ask before upgrading, then: agentrecall upgrade --apply"
    )


def notice_if_outdated(
    layout: Layout,
    *,
    current: str = __version__,
    fetch: Callable[[], str | None] | None = None,
) -> str | None:
    if os.environ.get(SKIP_ENV, "").strip():
        return None
    latest = cached_latest(layout, fetch=fetch)
    if latest is None:
        return None
    if not is_newer(latest, current):
        return None
    return outdated_notice(current, latest)


def upgrade_plan(
    *,
    current: str = __version__,
    latest: str | None,
) -> tuple[list[str], bool]:
    if latest is None:
        return ["could not check PyPI for a newer release"], True
    if not is_newer(latest, current):
        return [f"already latest ({current})"], False
    return [
        f"would upgrade agentrecall {current} -> {latest}",
        f"command: {' '.join(upgrade_argv(latest))}",
    ], False


def apply_upgrade(latest: str) -> str:
    argv = upgrade_argv(latest)
    uv = shutil.which(argv[0])
    if uv is None:
        raise FileNotFoundError("uv is not on PATH")
    try:
        result = subprocess.run(
            [uv, *argv[1:]],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(detail or f"uv tool install failed with {exc.returncode}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("uv tool install timed out") from exc
    output = (result.stdout or "").strip()
    if output:
        return output
    return f"installed agentrecall {latest}"
