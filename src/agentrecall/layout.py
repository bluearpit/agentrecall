"""Home-directory layout for supported coding agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import environ
from pathlib import Path


class AgentName(StrEnum):
    claude = "claude"
    cursor = "cursor"
    codex = "codex"
    opencode = "opencode"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: AgentName
    display_name: str
    home: Path
    skills_dir: Path
    reads_agents_skills_natively: bool
    instruction_file: Path | None
    instruction_kind: str | None
    transcripts_glob: str
    detect_path: Path


@dataclass(frozen=True, slots=True)
class Layout:
    home: Path
    env: Mapping[str, str]

    @classmethod
    def from_environ(cls, env: Mapping[str, str] | None = None) -> Layout:
        mapping = environ if env is None else env
        if "HOME" in mapping and mapping["HOME"]:
            home = Path(mapping["HOME"]).expanduser()
        else:
            home = Path.home()
        return cls(home=home, env=mapping)

    @property
    def agents_home(self) -> Path:
        return self.home / ".agents"

    @property
    def agents_skills(self) -> Path:
        return self.agents_home / "skills"

    @property
    def agents_instructions(self) -> Path:
        return self.agents_home / "AGENTS.md"

    @property
    def permissions_file(self) -> Path:
        return self.agents_home / "permissions.yaml"

    @property
    def permissions_managed_file(self) -> Path:
        return self.agents_home / "permissions.managed.json"

    @property
    def history_dir(self) -> Path:
        return self.agents_home / "history"

    @property
    def history_db(self) -> Path:
        return self.history_dir / "index.sqlite"

    @property
    def xdg_config_home(self) -> Path:
        configured = self.env.get("XDG_CONFIG_HOME", "").strip()
        if configured:
            return Path(configured).expanduser()
        return self.home / ".config"

    @property
    def claude_home(self) -> Path:
        configured = self.env.get("CLAUDE_CONFIG_DIR", "").strip()
        if configured:
            return Path(configured).expanduser()
        return self.home / ".claude"

    @property
    def codex_home(self) -> Path:
        configured = self.env.get("CODEX_HOME", "").strip()
        if configured:
            return Path(configured).expanduser()
        return self.home / ".codex"

    @property
    def cursor_home(self) -> Path:
        return self.home / ".cursor"

    @property
    def opencode_home(self) -> Path:
        return self.xdg_config_home / "opencode"

    def agent(self, name: AgentName) -> AgentSpec:
        if name is AgentName.claude:
            home = self.claude_home
            return AgentSpec(
                name=name,
                display_name="Claude Code",
                home=home,
                skills_dir=home / "skills",
                reads_agents_skills_natively=False,
                instruction_file=home / "CLAUDE.md",
                instruction_kind="CLAUDE.md",
                transcripts_glob="projects/*/*.jsonl",
                detect_path=home,
            )
        if name is AgentName.cursor:
            home = self.cursor_home
            return AgentSpec(
                name=name,
                display_name="Cursor",
                home=home,
                skills_dir=home / "skills",
                reads_agents_skills_natively=True,
                instruction_file=None,
                instruction_kind=None,
                transcripts_glob="projects/*/agent-transcripts/*/*.jsonl",
                detect_path=home,
            )
        if name is AgentName.codex:
            home = self.codex_home
            return AgentSpec(
                name=name,
                display_name="Codex",
                home=home,
                skills_dir=home / "skills",
                reads_agents_skills_natively=True,
                instruction_file=home / "AGENTS.md",
                instruction_kind="AGENTS.md",
                transcripts_glob="sessions/**/*.jsonl",
                detect_path=home,
            )
        home = self.opencode_home
        return AgentSpec(
            name=name,
            display_name="OpenCode",
            home=home,
            skills_dir=home / "skills",
            reads_agents_skills_natively=True,
            instruction_file=None,
            instruction_kind=None,
            transcripts_glob="",
            detect_path=home,
        )

    def agents(self) -> tuple[AgentSpec, ...]:
        return tuple(self.agent(name) for name in AgentName)

    def is_installed(self, spec: AgentSpec) -> bool:
        return spec.detect_path.exists()
