from __future__ import annotations

import base64
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = tmp_path / "chat.sqlite3"
    monkeypatch.setattr(app_module, "DB_PATH", str(database))
    monkeypatch.setattr(app_module, "WORKSPACE_ROOT", tmp_path)
    app_module.PENDING_WORKSPACE_EDITS.clear()
    app_module.REQUEST_TIMES.clear()
    app_module.COMMAND_JOBS.clear()
    with TestClient(app_module.app) as test_client:
        yield test_client


def create_conversation(
    client: TestClient, project_root: Path, workspace_type: str = "chat"
) -> str:
    response = client.post(
        "/api/conversations",
        json={
            "model": "test-model",
            "project_root": str(project_root),
            "workspace_type": workspace_type,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_conversations_keep_project_file_access_isolated(
    client: TestClient, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.py").write_text("one\n", encoding="utf-8")
    (second / "two.py").write_text("two\n", encoding="utf-8")

    first_id = create_conversation(client, first, "coding")
    second_id = create_conversation(client, second, "coding")

    first_tree = client.get("/api/workspace/tree", params={"conv_id": first_id})
    second_tree = client.get("/api/workspace/tree", params={"conv_id": second_id})

    assert [item["path"] for item in first_tree.json()["files"]] == ["one.py"]
    assert [item["path"] for item in second_tree.json()["files"]] == ["two.py"]
    assert first_tree.json()["project_root"] == str(first.resolve())


def test_chat_and_coding_conversations_are_listed_separately(
    client: TestClient, tmp_path: Path
) -> None:
    chat_id = create_conversation(client, tmp_path, "chat")
    coding_id = create_conversation(client, tmp_path, "coding")

    chats = client.get("/api/conversations", params={"workspace_type": "chat"})
    coding = client.get("/api/conversations", params={"workspace_type": "coding"})

    assert [row["id"] for row in chats.json()] == [chat_id]
    assert [row["id"] for row in coding.json()] == [coding_id]
    assert chats.json()[0]["workspace_type"] == "chat"
    assert coding.json()[0]["workspace_type"] == "coding"
    assert len(client.get("/api/conversations").json()) == 2


def test_existing_conversation_can_be_moved_between_workspaces(
    client: TestClient, tmp_path: Path
) -> None:
    conv_id = create_conversation(client, tmp_path)

    moved = client.post(
        f"/api/conversations/{conv_id}/workspace",
        json={"workspace_type": "coding"},
    )
    chats = client.get("/api/conversations", params={"workspace_type": "chat"})
    coding = client.get("/api/conversations", params={"workspace_type": "coding"})

    assert moved.status_code == 200
    assert moved.json()["workspace_type"] == "coding"
    assert moved.json()["project_root"] is None
    assert chats.json() == []
    assert [row["id"] for row in coding.json()] == [conv_id]
    assert (
        client.get("/api/workspace/tree", params={"conv_id": conv_id}).status_code
        == 400
    )


def test_new_coding_chat_requires_its_own_project(
    client: TestClient, tmp_path: Path
) -> None:
    project = tmp_path / "chosen-project"
    project.mkdir()
    (project / "main.py").write_text("print('project one')\n", encoding="utf-8")
    response = client.post(
        "/api/conversations",
        json={"model": "test-model", "workspace_type": "coding"},
    )
    assert response.status_code == 200
    coding_id = response.json()["id"]
    assert response.json()["project_root"] is None
    assert (
        client.get("/api/workspace/tree", params={"conv_id": coding_id}).status_code
        == 400
    )

    opened = client.post(
        f"/api/conversations/{coding_id}/project",
        json={"path": str(project)},
    )
    assert opened.status_code == 200
    tree = client.get("/api/workspace/tree", params={"conv_id": coding_id})
    assert tree.status_code == 200
    assert [item["path"] for item in tree.json()["files"]] == ["main.py"]


def test_chat_workspace_cannot_open_or_read_a_project(
    client: TestClient, tmp_path: Path
) -> None:
    conv_id = create_conversation(client, tmp_path, "chat")

    opened = client.post(
        f"/api/conversations/{conv_id}/project",
        json={"path": str(tmp_path)},
    )
    tree = client.get("/api/workspace/tree", params={"conv_id": conv_id})

    assert opened.status_code == 409
    assert tree.status_code == 409


def test_coding_chat_cannot_send_until_project_is_selected(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/conversations",
        json={"model": "test-model", "workspace_type": "coding"},
    )
    coding_id = response.json()["id"]

    chat_response = client.post(
        "/api/chat",
        json={
            "conv_id": coding_id,
            "content": "Inspect this project",
            "model": "test-model",
            "coding_mode": False,
        },
    )

    assert chat_response.status_code == 400
    assert "Open a project" in chat_response.json()["detail"]
    assert client.get(f"/api/conversations/{coding_id}").json()["messages"] == []


def test_switching_to_chat_removes_coding_project_access(
    client: TestClient, tmp_path: Path
) -> None:
    coding_id = create_conversation(client, tmp_path, "coding")

    moved = client.post(
        f"/api/conversations/{coding_id}/workspace",
        json={"workspace_type": "chat"},
    )
    conversation = client.get(f"/api/conversations/{coding_id}").json()["conversation"]
    tree = client.get("/api/workspace/tree", params={"conv_id": coding_id})

    assert moved.status_code == 200
    assert moved.json()["project_root"] is None
    assert conversation["project_root"] is None
    assert tree.status_code == 409


def test_invalid_workspace_filter_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/api/conversations", params={"workspace_type": "unsupported"}
    )
    assert response.status_code == 422


def test_conversation_list_shows_per_chat_token_usage(
    client: TestClient, tmp_path: Path
) -> None:
    conv_id = create_conversation(client, tmp_path)
    with app_module.db() as connection:
        connection.executemany(
            "INSERT INTO messages(conv_id,role,content,tokens,created_at) "
            "VALUES(?,?,?,?,?)",
            [
                (conv_id, "user", "question", 12, 1),
                (conv_id, "assistant", "answer", 34, 2),
            ],
        )

    conversation = next(
        row for row in client.get("/api/conversations").json() if row["id"] == conv_id
    )

    assert conversation["tokens"] == 46
    assert conversation["input_tokens"] == 12
    assert conversation["output_tokens"] == 34


def test_existing_database_schema_is_migrated_without_losing_chats(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE conversations("
            "id TEXT PRIMARY KEY,title TEXT,model TEXT,created_at REAL,updated_at REAL)"
        )
        connection.execute(
            "INSERT INTO conversations VALUES('legacy','Old chat','model',1,2)"
        )
        connection.execute(
            "CREATE TABLE messages("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,conv_id TEXT NOT NULL,"
            "role TEXT NOT NULL,content TEXT NOT NULL,tokens INTEGER DEFAULT 0,"
            "created_at REAL)"
        )
        connection.execute(
            "INSERT INTO messages(conv_id,role,content,tokens,created_at) "
            "VALUES('legacy','user','Keep this message',4,1)"
        )

    monkeypatch.setattr(app_module, "DB_PATH", str(database))
    app_module.init_db()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversations)")
        }
        conversation = connection.execute(
            "SELECT title,project_root FROM conversations WHERE id='legacy'"
        ).fetchone()
        message = connection.execute(
            "SELECT content FROM messages WHERE conv_id='legacy'"
        ).fetchone()
    assert {"provider_id", "project_root", "workspace_type"} <= columns
    assert conversation == ("Old chat", None)
    assert connection.execute(
        "SELECT workspace_type FROM conversations WHERE id='legacy'"
    ).fetchone() == ("chat",)
    assert message == ("Keep this message",)


def test_approved_edit_is_persisted_and_can_be_undone(
    client: TestClient, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "sample.py"
    target.write_text("before\n", encoding="utf-8")
    conv_id = create_conversation(client, project, "coding")

    preview = client.post(
        "/api/workspace/preview",
        json={
            "conv_id": conv_id,
            "changes": [{"path": "sample.py", "content": "after\n"}],
        },
    )
    assert preview.status_code == 200
    applied = client.post(
        f"/api/workspace/{preview.json()['review_id']}/apply",
        json={"approved": True},
    )
    assert applied.status_code == 200
    assert target.read_text(encoding="utf-8") == "after\n"
    edits = client.get(f"/api/conversations/{conv_id}/edits")
    assert len(edits.json()) == 1

    edit_id = edits.json()[0]["id"]
    undone = client.post(
        f"/api/conversations/{conv_id}/edits/{edit_id}/undo",
        json={"approved": True},
    )
    assert undone.status_code == 200
    assert target.read_text(encoding="utf-8") == "before\n"
    assert client.get(f"/api/conversations/{conv_id}/edits").json()[0]["undone_at"]


def test_project_selection_rejects_missing_directory(client: TestClient) -> None:
    conv_id = create_conversation(client, Path(app_module.WORKSPACE_ROOT))
    response = client.post(
        f"/api/conversations/{conv_id}/project",
        json={"path": str(Path(app_module.WORKSPACE_ROOT) / "missing")},
    )
    assert response.status_code == 400


def test_workspace_paths_and_commands_reject_unsafe_input(
    client: TestClient, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    conv_id = create_conversation(client, project, "coding")

    file_response = client.get(
        "/api/workspace/file",
        params={"conv_id": conv_id, "path": "..\\outside.txt"},
    )
    command_response = client.post(
        f"/api/conversations/{conv_id}/commands",
        json={"command": "python -c print(1)", "approved": True},
    )

    assert file_response.status_code == 400
    assert command_response.status_code == 403


def test_stale_edit_preview_does_not_overwrite_newer_file(
    client: TestClient, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "sample.py"
    target.write_text("before\n", encoding="utf-8")
    conv_id = create_conversation(client, project, "coding")
    preview = client.post(
        "/api/workspace/preview",
        json={
            "conv_id": conv_id,
            "changes": [{"path": "sample.py", "content": "assistant version\n"}],
        },
    )
    target.write_text("user change\n", encoding="utf-8")

    applied = client.post(
        f"/api/workspace/{preview.json()['review_id']}/apply",
        json={"approved": True},
    )

    assert applied.status_code == 409
    assert target.read_text(encoding="utf-8") == "user change\n"


def test_optional_basic_auth_rejects_bad_credentials(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(app_module, "ADMIN_USER", "local-user")
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "a-strong-local-password")

    assert client.get("/api/conversations").status_code == 401
    malformed = client.get("/api/conversations", headers={"Authorization": "Basic %%%"})
    assert malformed.status_code == 401

    credentials = base64.b64encode(b"local-user:a-strong-local-password").decode(
        "ascii"
    )
    authenticated = client.get(
        "/api/conversations", headers={"Authorization": f"Basic {credentials}"}
    )
    assert authenticated.status_code == 200


def test_sliding_window_rate_limit_blocks_request_61() -> None:
    app_module.REQUEST_TIMES.clear()
    for _ in range(60):
        app_module.enforce_rate_limit("rate-limited-test")
    with pytest.raises(HTTPException) as error:
        app_module.enforce_rate_limit("rate-limited-test")
    assert error.value.status_code == 429
    app_module.REQUEST_TIMES.clear()


def test_approved_allowlisted_command_runs_in_project(
    client: TestClient, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    conv_id = create_conversation(client, project, "coding")

    queued = client.post(
        f"/api/conversations/{conv_id}/commands",
        json={"command": "python -m compileall .", "approved": True},
    )
    assert queued.status_code == 200
    status = None
    for _ in range(50):
        response = client.get(
            f"/api/commands/{queued.json()['job_id']}",
            params={"conv_id": conv_id},
        )
        status = response.json()
        if status["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)

    assert status is not None
    assert status["status"] == "completed"
    assert status["exit_code"] == 0


def test_provider_test_never_returns_api_keys(client: TestClient, monkeypatch) -> None:
    async def fake_fetch_models(base_url: str, key: str, name: str) -> list[str]:
        assert key in {"saved-secret-marker", "submitted-secret-marker"}
        return ["test-model"]

    monkeypatch.setattr(app_module, "fetch_models", fake_fetch_models)
    created = client.post(
        "/api/providers",
        json={
            "name": "Test provider",
            "base_url": "https://provider.example/v1",
            "api_key": "saved-secret-marker",
        },
    )
    assert created.status_code == 200
    provider_id = created.json()["id"]

    saved_key_test = client.post(
        "/api/providers/test",
        json={
            "base_url": "https://provider.example/v1",
            "provider_id": provider_id,
        },
    )
    submitted_key_test = client.post(
        "/api/providers/test",
        json={
            "base_url": "https://provider.example/v1",
            "api_key": "submitted-secret-marker",
        },
    )

    assert saved_key_test.status_code == 200
    assert submitted_key_test.status_code == 200
    assert saved_key_test.json() == {"ok": True, "models": ["test-model"]}
    assert submitted_key_test.json() == {"ok": True, "models": ["test-model"]}
    assert "saved-secret-marker" not in saved_key_test.text
    assert "submitted-secret-marker" not in submitted_key_test.text
