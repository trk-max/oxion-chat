"""Auto-discovery and validation for skill directories."""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from backend.skills.base import BaseSkill, SkillConfig

logger = logging.getLogger("my_only_chat.skills")
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_ALLOWED_TOOLS = {
    "read_file",
    "write_file",
    "run_command",
    "list_files",
    "create_diff",
}


class ConfiguredSkill(BaseSkill):
    """Fallback skill for a configuration without a Python implementation."""

    @property
    def prompt(self) -> str:
        return (
            f"You are the {self.config.name} skill. "
            f"{self.config.description}\n{self.config.instructions}"
        ).strip()


def parse_skill_config(path: Path) -> SkillConfig:
    """Load and validate a YAML skill configuration."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a YAML object")

    name = raw.get("name")
    description = raw.get("description")
    triggers = raw.get("triggers", [])
    tools = raw.get("tools", [])
    examples = raw.get("examples", [])
    enabled = raw.get("enabled", True)
    priority = raw.get("priority", 0)
    instructions = raw.get("instructions", "")

    if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError("name must be a lowercase skill identifier")
    if name != path.parent.name:
        raise ValueError("name must match its skill directory")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    if not isinstance(triggers, list) or not all(
        isinstance(item, str) and item.strip() for item in triggers
    ):
        raise ValueError("triggers must be a list of non-empty strings")
    if not isinstance(tools, list) or not all(
        isinstance(item, str) and item in _ALLOWED_TOOLS for item in tools
    ):
        raise ValueError("tools must use the supported tool names")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("priority must be an integer")
    if not isinstance(examples, list) or not all(
        isinstance(item, str) for item in examples
    ):
        raise ValueError("examples must be a list of strings")
    if not isinstance(instructions, str):
        raise ValueError("instructions must be a string")

    return SkillConfig(
        name=name,
        description=description.strip(),
        triggers=[trigger.strip() for trigger in triggers],
        tools=list(tools),
        enabled=enabled,
        priority=priority,
        examples=list(examples),
        instructions=instructions.strip(),
    )


def _load_implementation(skill_dir: Path, config: SkillConfig) -> BaseSkill:
    """Import a trusted local skill implementation, or use its declarative prompt."""
    module_path = skill_dir / "skill.py"
    if not module_path.is_file():
        return ConfiguredSkill(config)
    module_name = f"_my_only_chat_skill_{config.name}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError("could not create a module loader")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    implementations = [
        value
        for value in vars(module).values()
        if inspect.isclass(value)
        and value is not BaseSkill
        and issubclass(value, BaseSkill)
        and value.__module__ == module_name
    ]
    if len(implementations) != 1:
        raise ValueError("skill.py must define exactly one BaseSkill subclass")
    skill = implementations[0](config)
    if not isinstance(skill.prompt, str) or not skill.prompt.strip():
        raise ValueError("skill prompt must be a non-empty string")
    return skill


def load_skill_directory(skill_dir: Path) -> tuple[BaseSkill, Path]:
    """Load a single skill and return it with its configuration path."""
    config_path = skill_dir / "config.yaml"
    config = parse_skill_config(config_path)
    return _load_implementation(skill_dir, config), config_path


def discover_skills(skills_dir: Path) -> list[tuple[BaseSkill, Path]]:
    """Discover valid skill folders; log and skip invalid folders."""
    discovered: list[tuple[BaseSkill, Path]] = []
    if not skills_dir.exists():
        return discovered
    for skill_dir in sorted(skills_dir.iterdir(), key=lambda item: item.name):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        try:
            discovered.append(load_skill_directory(skill_dir))
        except Exception as exc:
            logger.warning("Skipping skill %s: %s", skill_dir.name, exc)
    return discovered
