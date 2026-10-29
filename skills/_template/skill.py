"""Starter implementation for a custom assistant skill."""

from backend.skills import BaseSkill


class CustomSkill(BaseSkill):
    """Replace this class with a specialization for the new skill."""

    @property
    def prompt(self) -> str:
        return (
            f"You are the {self.config.name} skill. "
            f"{self.config.description}\n{self.config.instructions}"
        )
