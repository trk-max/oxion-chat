"""Prompt specialization for test-writing tasks."""

from backend.skills import BaseSkill


class TestingSkill(BaseSkill):
    """Guide test work toward reproducible behavior and clear assertions."""

    @property
    def prompt(self) -> str:
        return (
            "You are the testing skill. First identify the project's existing "
            "test framework and nearby test patterns. Write focused tests for "
            "success paths, error paths, and important boundaries. Never invent "
            "test results; running commands requires explicit user approval."
        )
