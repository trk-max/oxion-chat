"""Common types and behavior for assistant skills."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SkillConfig:
    """Validated configuration for one skill."""

    name: str
    description: str
    triggers: list[str]
    tools: list[str]
    enabled: bool = True
    priority: int = 0
    examples: list[str] | None = None
    instructions: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the public, non-secret skill configuration."""
        return {
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "tools": list(self.tools),
            "enabled": self.enabled,
            "priority": self.priority,
            "examples": list(self.examples or []),
        }


class BaseSkill(ABC):
    """Base interface for prompt-specialized skills."""

    def __init__(self, config: SkillConfig) -> None:
        self.config = config

    @property
    @abstractmethod
    def prompt(self) -> str:
        """Return the system instructions for this skill."""

    def can_handle(self, task: str) -> bool:
        """Return whether a configured trigger appears as a token in the task."""
        normalized = task.casefold()
        for trigger in self.config.triggers:
            candidate = trigger.casefold().strip()
            if not candidate:
                continue
            pattern = rf"(?<!\w){re.escape(candidate)}(?!\w)"
            if re.search(pattern, normalized):
                return True
        return False

    def execute(self, task: str, context: dict[str, Any]) -> str:
        """Prepare skill instructions for the existing approval-based chat flow."""
        safe_context = {
            key: value
            for key, value in context.items()
            if key not in {"api_key", "api_keys", "secrets", "environment"}
        }
        return f"{self.prompt}\n\nTask:\n{task}\n\n" f"Context:\n{safe_context}"
