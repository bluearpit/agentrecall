"""Run against a built wheel or sdist, not the checkout source tree."""

from __future__ import annotations

from agentrecall import __version__
from agentrecall.cli import app
from agentrecall.skills import bundled_skills_dir


def main() -> None:
    if not __version__:
        raise SystemExit("missing version")
    if app is None:
        raise SystemExit("missing CLI app")
    skill = bundled_skills_dir() / "search-project-history" / "SKILL.md"
    if not skill.is_file():
        raise SystemExit(f"bundled skill missing: {skill}")
    print(f"ok {__version__} {skill}")


if __name__ == "__main__":
    main()
