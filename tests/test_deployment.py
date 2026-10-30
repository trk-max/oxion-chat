from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "hosted-test.sqlite3"))
    monkeypatch.setattr(app_module, "WORKSPACE_ROOT", tmp_path)
    with TestClient(app_module.app) as test_client:
        yield test_client


class RecordingConnection:
    def __init__(self) -> None:
        self.query: str | None = None
        self.parameters: tuple | list = ()

    def execute(self, query: str, parameters: tuple | list = ()) -> RecordingConnection:
        self.query = query
        self.parameters = parameters
        return self

    def fetchall(self) -> list:
        return []


def test_postgres_connection_translates_sqlite_placeholders() -> None:
    connection = RecordingConnection()
    postgres = app_module.PostgresConnection(connection)

    result = postgres.execute(
        "SELECT * FROM messages WHERE conv_id=? AND role=?", ("chat", "user")
    )

    assert result is connection
    assert connection.query == ("SELECT * FROM messages WHERE conv_id=%s AND role=%s")
    assert connection.parameters == ("chat", "user")


def test_postgres_connection_translates_insert_or_ignore() -> None:
    connection = RecordingConnection()
    postgres = app_module.PostgresConnection(connection)

    postgres.execute("INSERT OR IGNORE INTO settings(key) VALUES(?)", ("ready",))

    assert connection.query == (
        "INSERT INTO settings(key) VALUES(%s) ON CONFLICT DO NOTHING"
    )


def test_conversation_list_omits_nullable_postgres_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = RecordingConnection()

    @contextmanager
    def fake_db() -> Generator[Any, None, None]:
        yield app_module.PostgresConnection(connection)

    monkeypatch.setattr(app_module, "db", fake_db)

    response = client.get("/api/conversations")

    assert response.status_code == 200
    assert "WHERE c.workspace_type" not in connection.query
    assert "IS NULL" not in connection.query
    assert connection.parameters == ()


def test_conversation_list_applies_typed_workspace_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = RecordingConnection()

    @contextmanager
    def fake_db() -> Generator[Any, None, None]:
        yield app_module.PostgresConnection(connection)

    monkeypatch.setattr(app_module, "db", fake_db)

    response = client.get("/api/conversations", params={"workspace_type": "chat"})

    assert response.status_code == 200
    assert "WHERE c.workspace_type=%s" in connection.query
    assert connection.parameters == ("chat",)


def test_hosted_app_rejects_coding_workspaces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "IS_VERCEL", True)

    response = client.post(
        "/api/conversations",
        json={"model": "test-model", "workspace_type": "coding"},
    )

    assert response.status_code == 501
    assert "cannot access folders on your computer" in response.json()["detail"]
