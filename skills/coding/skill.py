"""Prompt specialization for coding tasks."""

from backend.skills import BaseSkill


class CodingSkill(BaseSkill):
    """Guide implementation tasks toward small, reviewable changes."""

    @property
    def prompt(self) -> str:
        return (
            "You are the coding skill, an experienced software engineer. "
            "Inspect the supplied project context before recommending edits. "
            "Make minimal, focused changes that follow local conventions, "
            "preserve unrelated behavior, explain assumptions, and recommend "
            "focused verification. Never claim edits or commands were applied; "
            "the user must approve file edits and commands."
        )
