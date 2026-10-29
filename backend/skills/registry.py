"""Skill registration, activation, matching, and execution."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

import yaml  # type: ignore[import-untyped]

from backend.skills.base import BaseSkill, SkillConfig
from backend.skills.loader import discover_skills

logger = logging.getLogger("my_only_chat.skills")


class SkillRegistry:
    """Manage discovered skills and persist their enabled state in YAML."""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir.resolve()
        self._skills: dict[str, BaseSkill] = {}
        self._config_paths: dict[str, Path] = {}
        self._lock = RLock()
        self.reload()

    def reload(self) -> None:
        """Discover all valid skills from the configured directory."""
        loaded = discover_skills(self.skills_dir)
        with self._lock:
            self._skills = {skill.config.name: skill for skill, _ in loaded}
            self._config_paths = {skill.config.name: path for skill, path in loaded}

    def list_skills(self) -> list[SkillConfig]:
        """Return skill configuration records ordered by name."""
        with self._lock:
            return [self._skills[name].config for name in sorted(self._skills)]

    def get_skill(self, name: str, *, require_enabled: bool = False) -> BaseSkill:
        """Get a discovered skill, optionally requiring it to be enabled."""
        with self._lock:
            skill = self._skills.get(name)
            if skill is None:
                raise KeyError(f"Unknown skill: {name}")
            if require_enabled and not skill.config.enabled:
                raise PermissionError(f"Skill is disabled: {name}")
            return skill

    def activate_skill(self, name: str) -> None:
        """Enable a skill and persist the setting to its configuration file."""
        self._set_enabled(name, True)

    def deactivate_skill(self, name: str) -> None:
        """Disable a skill and persist the setting to its configuration file."""
        self._set_enabled(name, False)

    def _set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            skill = self._skills.get(name)
            config_path = self._config_paths.get(name)
            if skill is None or config_path is None:
                raise KeyError(f"Unknown skill: {name}")
            raw: dict[str, Any] = yaml.safe_load(
                config_path.read_text(encoding="utf-8")
            )
            raw["enabled"] = enabled
            content = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=config_path.parent,
                prefix=f".{config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(content)
                temporary = Path(temp_file.name)
            try:
                os.replace(temporary, config_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            skill.config.enabled = enabled
        logger.info("Skill %s %s", name, "activated" if enabled else "deactivated")

    def get_enabled_skills(self) -> list[BaseSkill]:
        """Return enabled skills in deterministic priority order."""
        with self._lock:
            return sorted(
                (skill for skill in self._skills.values() if skill.config.enabled),
                key=lambda skill: (-skill.config.priority, skill.config.name),
            )

    def match_skills(
        self, task: str, preferred_name: str | None = None
    ) -> list[BaseSkill]:
        """Match enabled skills to a task, honoring an explicit selection."""
        if preferred_name:
            return [self.get_skill(preferred_name, require_enabled=True)]
        return [skill for skill in self.get_enabled_skills() if skill.can_handle(task)]

    def execute_skill(self, skill_name: str, task: str, context: dict[str, Any]) -> str:
        """Prepare a skill prompt and task context for an approval-based workflow."""
        skill = self.get_skill(skill_name, require_enabled=True)
        logger.info("Preparing task with skill %s", skill_name)
        return skill.execute(task, context)
