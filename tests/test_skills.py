from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from backend.skills import SkillRegistry
from backend.skills.base import SkillConfig
from backend.skills.loader import ConfiguredSkill, parse_skill_config
from scripts import create_skill as create_skill_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_builtin_skills_are_discovered_and_ordered() -> None:
    registry = SkillRegistry(PROJECT_ROOT / "skills")

    assert [config.name for config in registry.list_skills()] == [
        "coding",
        "debug",
        "testing",
    ]
    assert [skill.config.name for skill in registry.match_skills("@test this")] == [
        "testing"
    ]
    assert [
        skill.config.name for skill in registry.match_skills("debug this error")
    ] == ["debug"]
    assert registry.match_skills("talk about decoding") == []


def test_skill_activation_persists_to_config_file(tmp_path: Path) -> None:
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    (skill_dir / "config.yaml").write_text(
        "name: example\ndescription: Example\ntriggers: [example]\n"
        "tools: [read_file]\nenabled: true\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    registry.deactivate_skill("example")
    assert registry.get_skill("example").config.enabled is False
    assert "enabled: false" in (skill_dir / "config.yaml").read_text(encoding="utf-8")
    assert registry.match_skills("example") == []

    registry.activate_skill("example")
    assert registry.get_skill("example").config.enabled is True
    assert registry.match_skills("example")[0].config.name == "example"


def test_skill_prompt_context_excludes_secrets() -> None:
    config = SkillConfig(
        name="example",
        description="A test skill",
        triggers=["@example"],
        tools=[],
        instructions="Use the supplied evidence.",
    )
    skill = ConfiguredSkill(config)

    result = skill.execute(
        "Explain the bug",
        {"workspace_type": "coding", "api_key": "do-not-return"},
    )

    assert "Explain the bug" in result
    assert "workspace_type" in result
    assert "do-not-return" not in result


def test_loader_skips_invalid_skill_config(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "config.yaml").write_text(
        "name: mismatch\ndescription: Broken\n", encoding="utf-8"
    )

    assert SkillRegistry(tmp_path).list_skills() == []


def test_create_skill_copies_template_and_supports_dry_run(
    tmp_path: Path, monkeypatch
) -> None:
    skills_dir = tmp_path / "skills"
    shutil.copytree(PROJECT_ROOT / "skills" / "_template", skills_dir / "_template")
    monkeypatch.setattr(create_skill_module, "SKILLS_DIR", skills_dir)

    destination = create_skill_module.create_skill("review_helper", dry_run=True)
    assert not destination.exists()

    created = create_skill_module.create_skill("review_helper")
    registry = SkillRegistry(skills_dir)
    assert created == destination
    assert registry.get_skill("review_helper").config.triggers == ["@review_helper"]


def test_skills_api_lists_and_toggles_isolated_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "skills.sqlite3"))
    skills_dir = tmp_path / "skills"
    shutil.copytree(PROJECT_ROOT / "skills", skills_dir)
    monkeypatch.setattr(
        app_module,
        "SKILL_REGISTRY",
        SkillRegistry(skills_dir),
    )
    with TestClient(app_module.app) as client:
        listing = client.get("/api/skills")
        assert listing.status_code == 200
        assert {skill["name"] for skill in listing.json()} == {
            "coding",
            "testing",
            "debug",
        }

        deactivated = client.post("/api/skills/testing/deactivate")
        assert deactivated.status_code == 200
        assert deactivated.json()["enabled"] is False
        assert client.get("/api/skills").json()[2]["enabled"] is False

        activated = client.post("/api/v1/skills/testing/activate")
        assert activated.status_code == 200
        assert activated.json()["enabled"] is True


def test_skills_execute_api_prepares_prompt_without_applying_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / "skills"
    shutil.copytree(PROJECT_ROOT / "skills", skills_dir)
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "execute.sqlite3"))
    monkeypatch.setattr(app_module, "SKILL_REGISTRY", SkillRegistry(skills_dir))

    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/v1/skills/execute",
            json={"skill_name": "coding", "task": "Refactor this function"},
        )
        assert response.status_code == 200
        assert response.json()["skill"] == "coding"
        assert "Refactor this function" in response.json()["prepared_prompt"]

        client.post("/api/skills/coding/deactivate")
        disabled = client.post(
            "/api/skills/execute",
            json={"skill_name": "coding", "task": "Refactor this function"},
        )
        assert disabled.status_code == 409


def test_skill_config_rejects_unsupported_tools(tmp_path: Path) -> None:
    skill_dir = tmp_path / "invalid_tool"
    skill_dir.mkdir()
    config = skill_dir / "config.yaml"
    config.write_text(
        "name: invalid_tool\ndescription: Invalid\ntriggers: [invalid]\n"
        "tools: [arbitrary_shell]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="supported tool names"):
        parse_skill_config(config)
