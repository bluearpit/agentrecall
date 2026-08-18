from __future__ import annotations

from pathlib import Path

from agentrecall.layout import Layout
from agentrecall.linking import is_linked_to
from agentrecall.skills import bundled_skills_dir, extra_claude_skills, skill_dirs, sync_skills


def _write_skill(root: Path, name: str, body: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    return skill


def test_skills_symlink_and_skip_foreign(home: Path, layout: Layout) -> None:
    canonical = _write_skill(layout.agents_skills, "pr-review", "# review\n")
    claude_skills = home / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    other = home / "web-scraper"
    other.mkdir()
    (other / "SKILL.md").write_text("# other\n", encoding="utf-8")
    (claude_skills / "web-scraper").symlink_to(other)

    lines, conflicts = sync_skills(layout, dry_run=False)
    assert conflicts == 0
    linked = claude_skills / "pr-review"
    assert is_linked_to(linked, canonical)
    extras = extra_claude_skills(layout)
    assert any(path.name == "web-scraper" for path in extras)
    assert "web-scraper" not in {path.name for path in skill_dirs(layout.agents_skills)}
    assert any("pr-review" in line for line in lines)


def test_broken_skill_symlink_is_replaced(home: Path, layout: Layout) -> None:
    canonical = _write_skill(layout.agents_skills, "demo", "# demo\n")
    claude_skills = home / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    (claude_skills / "demo").symlink_to(home / "gone" / "demo")

    lines, conflicts = sync_skills(layout, dry_run=False)
    assert conflicts == 0
    assert is_linked_to(claude_skills / "demo", canonical)
    assert any("demo" in line for line in lines)


def test_broken_bundled_skill_symlink_is_replaced(home: Path, layout: Layout) -> None:
    dest = layout.agents_skills / "search-project-history"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(home / "gone" / "search-project-history")
    lines, conflicts = sync_skills(layout, dry_run=False)
    assert conflicts == 0
    assert is_linked_to(dest, bundled_skills_dir() / "search-project-history")
    assert any("search-project-history" in line for line in lines)


def test_identical_copy_replaced_stale_conflicts(home: Path, layout: Layout) -> None:
    canonical = _write_skill(layout.agents_skills, "same", "# same\n")
    _write_skill(layout.agents_skills, "stale", "# new\n")
    claude_skills = home / ".claude" / "skills"
    same_copy = _write_skill(claude_skills, "same", "# same\n")
    stale_copy = _write_skill(claude_skills, "stale", "# old\n")

    lines, conflicts = sync_skills(layout, dry_run=False)
    assert conflicts == 1
    assert is_linked_to(same_copy, canonical)
    assert stale_copy.is_dir() and not stale_copy.is_symlink()
    assert any("conflict" in line for line in lines)
