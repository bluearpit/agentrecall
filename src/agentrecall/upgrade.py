"""Check GitHub for a newer agentrecall release. Never install without --apply."""

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
RELEASES_URL = "https://api.github.com/repos/bluearpit/agentrecall/releases/latest"
REPO_GIT = "git+https://github.com/bluearpit/agentrecall.git"


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


def fetch_latest_version(url: str = RELEASES_URL) -> str | None:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
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
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return None
    try:
        parse_version(tag)
    except ValueError:
        return None
    return tag.lstrip("vV")


def update_check_file(layout: Layout) -> Path:
    return layout.agents_home / "update-check.json"


def cached_latest(
    layout: Layout,
    *,
    now: datetime | None = None,
    fetch: Callable[[], str | None] | None = None,
) -> str | None:
    moment = datetime.now(tz=UTC) if now is None else now
    path = update_check_file(layout)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        cached = payload.get("latest") if isinstance(payload, dict) else None
        checked_raw = payload.get("checked_at") if isinstance(payload, dict) else None
        if isinstance(cached, str) and isinstance(checked_raw, str):
            try:
                checked = datetime.fromisoformat(checked_raw)
            except ValueError:
                checked = None
            if checked is not None:
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=UTC)
                if moment - checked < CHECK_INTERVAL:
                    return cached
    getter = fetch if fetch is not None else fetch_latest_version
    latest = getter()
    if latest is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"latest": latest, "checked_at": moment.isoformat()}),
        encoding="utf-8",
    )
    return latest


def install_spec(latest: str) -> str:
    version = latest.strip().lstrip("vV")
    return f"{REPO_GIT}@v{version}"


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
        return ["could not check GitHub for a newer release"], True
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
