"""Configurable, locally loaded assistant skills."""

from backend.skills.base import BaseSkill, SkillConfig
from backend.skills.registry import SkillRegistry

__all__ = ["BaseSkill", "SkillConfig", "SkillRegistry"]
