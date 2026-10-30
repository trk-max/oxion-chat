import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.skills import BaseSkill, SkillRegistry

load_dotenv()

DB_PATH = os.getenv("CHAT_DB", "chat.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CHAT_ENCRYPTION_KEY = os.getenv("CHAT_ENCRYPTION_KEY", "").strip()
IS_VERCEL = os.getenv("VERCEL") == "1"
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "12000"))
WORKSPACE_ROOT = Path(__file__).resolve().parent
SKILL_REGISTRY = SkillRegistry(WORKSPACE_ROOT / "skills")
MAX_WORKSPACE_FILES = 100
MAX_WORKSPACE_FILE_BYTES = 256_000
MAX_WORKSPACE_CONTEXT_CHARS = 100_000
PENDING_WORKSPACE_EDITS: dict[str, dict[str, Any]] = {}
COMMAND_JOBS: dict[str, dict[str, Any]] = {}
COMMAND_JOBS_LOCK = threading.Lock()
REQUEST_TIMES: dict[str, deque[float]] = defaultdict(deque)
REQUEST_TIMES_LOCK = threading.Lock()
SAFE_COMMANDS = (
    ("python", "-m", "pytest"),
    ("python", "-m", "compileall"),
    ("pytest",),
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("ruff", "check"),
)
MAX_COMMAND_LENGTH = 1000
MAX_COMMAND_OUTPUT = 100_000
COMMAND_TIMEOUT_SECONDS = 300
logger = logging.getLogger("my_only_chat")
log_level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
log_level = getattr(logging, log_level_name, None)
if not isinstance(log_level, int):
    raise ValueError("LOG_LEVEL must be a valid Python logging level")
logger.setLevel(log_level)

ADMIN_USER = os.getenv("ADMIN_USER", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
CORS_ORIGINS = tuple(
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
)
if bool(ADMIN_USER) != bool(ADMIN_PASSWORD):
    raise ValueError("Set both ADMIN_USER and ADMIN_PASSWORD, or neither")
if APP_ENV == "production":
    if not ADMIN_USER or len(ADMIN_PASSWORD) < 16:
        raise ValueError(
            "Production requires ADMIN_USER and a 16-character ADMIN_PASSWORD"
        )
    if not DATABASE_URL:
        raise ValueError("Production requires DATABASE_URL")
    if sys.platform != "win32" and not CHAT_ENCRYPTION_KEY:
        raise ValueError("Production requires CHAT_ENCRYPTION_KEY")
    if "*" in CORS_ORIGINS:
        raise ValueError("Wildcard CORS origins are not allowed in production")
    if any(not origin.lower().startswith("https://") for origin in CORS_ORIGINS):
        raise ValueError("Production CORS origins must use HTTPS")

# Optional: on first start an Oxion provider is created from these, so the
# old PowerShell setup keeps working. After that, manage providers in the UI.
ENV_BASE_URL = os.getenv("OXION_BASE_URL", "https://oxionai.com/v1").rstrip("/")
ENV_API_KEY = os.getenv("OXION_API_KEY", "")


# ---------------------------------------------------------------- secrets
# API keys are encrypted before they touch SQLite. On Windows this uses DPAPI,
# which ties the ciphertext to your Windows account: copying chat.db to another
# machine or user does not reveal the keys.
_ENTROPY = b"oxion-chat/provider-key/v1"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def _dpapi(data: bytes, protect: bool) -> bytes:
        def blob(raw: bytes):
            buf = ctypes.create_string_buffer(raw, len(raw))
            return _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        blob_in, _keep_in = blob(data)
        entropy, _keep_ent = blob(_ENTROPY)
        blob_out = _Blob()
        fn = _crypt32.CryptProtectData if protect else _crypt32.CryptUnprotectData
        ok = fn(
            ctypes.byref(blob_in),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))

    def encrypt_secret(plain: str) -> str:
        return "dpapi:" + base64.b64encode(_dpapi(plain.encode(), True)).decode()

    def _decrypt_dpapi(token: str) -> str:
        return _dpapi(base64.b64decode(token), False).decode()

else:

    def encrypt_secret(plain: str) -> str:
        return "fernet:" + _fernet().encrypt(plain.encode()).decode()

    def _decrypt_dpapi(token: str) -> str:
        raise RuntimeError("Key was encrypted with Windows DPAPI on another system")


def _fernet():
    """Non-Windows fallback: needs `pip install cryptography`."""
    from cryptography.fernet import Fernet

    if CHAT_ENCRYPTION_KEY:
        return Fernet(CHAT_ENCRYPTION_KEY.encode("ascii"))
    key_path = os.getenv("CHAT_KEY_FILE", DB_PATH + ".key")
    if not os.path.exists(key_path):
        with open(key_path, "wb") as f:
            f.write(Fernet.generate_key())
        os.chmod(key_path, 0o600)
    with open(key_path, "rb") as f:
        return Fernet(f.read())


def decrypt_secret(stored: str | None) -> str:
    if not stored:
        return ""
    scheme, _, token = stored.partition(":")
    if scheme == "dpapi":
        return _decrypt_dpapi(token)
    if scheme == "fernet":
        return str(_fernet().decrypt(token.encode()).decode())
    raise RuntimeError("Unknown key encryption scheme")


def mask_key(plain: str) -> str:
    if not plain:
        return ""
    return "••••••••" + (plain[-4:] if len(plain) > 10 else "")


# ---------------------------------------------------------------- storage
class PostgresConnection:
    """Translate the app's small SQLite-style query surface for PostgreSQL."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, query: str, parameters: tuple | list = ()):
        """Execute parameterized SQL with PostgreSQL placeholders."""
        query = query.replace("?", "%s")
        if re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", query, re.IGNORECASE):
            query = (
                re.sub(
                    r"INSERT\s+OR\s+IGNORE\s+INTO",
                    "INSERT INTO",
                    query,
                    count=1,
                    flags=re.IGNORECASE,
                )
                .rstrip()
                .rstrip(";")
            )
            query += " ON CONFLICT DO NOTHING"
        return self._connection.execute(query, parameters)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


@contextmanager
def db():
    if DATABASE_URL:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL requires the 'psycopg[binary]' package"
            ) from exc
        with psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
            connect_timeout=10,
            prepare_threshold=None,
        ) as connection:
            yield PostgresConnection(connection)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        if DATABASE_URL:
            conn.execute("SELECT pg_advisory_xact_lock(724012894)")
            migration_table = (
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.execute(migration_table)
            migration_dir = Path(__file__).resolve().parent / "migrations"
            for migration in sorted(migration_dir.glob("*.sql")):
                applied = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?",
                    (migration.name,),
                ).fetchone()
                if applied:
                    continue
                for statement in migration.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES(?)",
                    (migration.name,),
                )
            cols = {
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='conversations'"
                )
            }
        else:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations(
                id         TEXT PRIMARY KEY,
                title      TEXT,
                model      TEXT,
                created_at REAL,
                updated_at REAL,
                workspace_type TEXT NOT NULL DEFAULT 'chat'
            );
            CREATE TABLE IF NOT EXISTS messages(
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id    TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                tokens     INTEGER DEFAULT 0,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id);
            CREATE TABLE IF NOT EXISTS providers(
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                base_url      TEXT NOT NULL,
                api_key_enc   TEXT,
                default_model TEXT,
                is_active     INTEGER DEFAULT 0,
                created_at    REAL,
                updated_at    REAL
            );
            CREATE TABLE IF NOT EXISTS settings(
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS provider_daily_usage(
                provider_id TEXT PRIMARY KEY,
                token_budget INTEGER NOT NULL DEFAULT 0,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                utc_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_edits(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                project_root TEXT NOT NULL,
                old_content TEXT,
                new_content TEXT NOT NULL,
                applied_at REAL NOT NULL,
                undone_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_edits_conv
                ON workspace_edits(conv_id, id DESC);
            """)
            cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(conversations)")
            }
        # Existing SQLite databases may predate provider and workspace columns.
        if "provider_id" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN provider_id TEXT")
        if "project_root" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN project_root TEXT")
        if "workspace_type" not in cols:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN workspace_type "
                "TEXT NOT NULL DEFAULT 'chat'"
            )

        today = utc_today()
        for provider in conn.execute("SELECT id, base_url FROM providers"):
            budget = default_daily_budget(provider["base_url"])
            usage_id = daily_usage_id(provider["id"], provider["base_url"])
            conn.execute(
                "INSERT OR IGNORE INTO provider_daily_usage"
                "(provider_id, token_budget, tokens_used, utc_date) VALUES(?,?,0,?)",
                (usage_id, budget, today),
            )

        # one-time import of the old environment-variable setup
        seeded = conn.execute(
            "SELECT value FROM settings WHERE key='seeded_from_env'"
        ).fetchone()
        if not seeded:
            if ENV_API_KEY:
                pid = uuid.uuid4().hex[:12]
                now = time.time()
                has_active = conn.execute(
                    "SELECT 1 FROM providers WHERE is_active=1"
                ).fetchone()
                conn.execute(
                    "INSERT INTO providers(id,name,base_url,api_key_enc,default_model,"
                    "is_active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        pid,
                        "Oxion AI",
                        ENV_BASE_URL,
                        encrypt_secret(ENV_API_KEY),
                        "gemma-2-2b",
                        0 if has_active else 1,
                        now,
                        now,
                    ),
                )
                # chats created before providers existed all went to Oxion
                conn.execute(
                    "UPDATE conversations SET provider_id=? WHERE provider_id IS NULL",
                    (pid,),
                )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('seeded_from_env', ?)",
                ("1" if ENV_API_KEY else "0",),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="My Only Chat", lifespan=lifespan)
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ORIGINS),
        allow_credentials=bool(ADMIN_USER),
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def protect_and_log_requests(request: Request, call_next):
    """Apply optional Basic auth and log request metadata without secrets."""
    started = time.monotonic()
    if ADMIN_USER:
        authorization = request.headers.get("authorization", "")
        try:
            scheme, encoded = authorization.split(" ", 1)
            supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = supplied.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            username, password, scheme = "", "", ""
        if (
            scheme.lower() != "basic"
            or not secrets.compare_digest(
                username.encode("utf-8"), ADMIN_USER.encode("utf-8")
            )
            or not secrets.compare_digest(
                password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")
            )
        ):
            response = JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="My Only Chat"'},
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    logger.info(
        "%s %s status=%s duration_ms=%d ip=%s user_agent=%r",
        request.method,
        request.url.path,
        response.status_code,
        round((time.monotonic() - started) * 1000),
        request.client.host if request.client else "unknown",
        request.headers.get("user-agent", "")[:300],
    )
    return response


# ---------------------------------------------------------------- helpers
def estimate_tokens(text: str) -> int:
    """Rough fallback when the API doesn't return usage (~4 chars/token)."""
    return max(1, len(text) // 4)


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def default_daily_budget(base_url: str) -> int:
    hostname = (urlparse(base_url).hostname or "").lower()
    return (
        20_000_000
        if hostname == "apinex.bond" or hostname.endswith(".apinex.bond")
        else 0
    )


def daily_usage_id(provider_id: str, base_url: str) -> str:
    return "apinex:shared" if default_daily_budget(base_url) else provider_id


def ensure_daily_usage(provider_id: str, base_url: str) -> dict:
    usage_id = daily_usage_id(provider_id, base_url)
    with db() as conn:
        return daily_usage_in_conn(conn, usage_id, base_url)


def daily_usage_in_conn(conn, provider_id: str, base_url: str) -> dict:
    today = utc_today()
    conn.execute(
        "INSERT OR IGNORE INTO provider_daily_usage"
        "(provider_id, token_budget, tokens_used, utc_date) VALUES(?,?,0,?)",
        (provider_id, default_daily_budget(base_url), today),
    )
    row = conn.execute(
        "SELECT * FROM provider_daily_usage WHERE provider_id=?", (provider_id,)
    ).fetchone()
    if row["utc_date"] != today:
        conn.execute(
            "UPDATE provider_daily_usage SET tokens_used=0, utc_date=? WHERE provider_id=?",
            (today, provider_id),
        )
        return {
            "token_budget": row["token_budget"],
            "tokens_used": 0,
            "utc_date": today,
        }
    return dict(row)


def record_daily_usage(provider_id: str, base_url: str, consumed_tokens: int):
    usage_id = daily_usage_id(provider_id, base_url)
    usage = ensure_daily_usage(provider_id, base_url)
    if consumed_tokens <= 0:
        return
    with db() as conn:
        conn.execute(
            "UPDATE provider_daily_usage SET tokens_used=tokens_used+? WHERE provider_id=? AND utc_date=?",
            (consumed_tokens, usage_id, usage["utc_date"]),
        )


def build_context(conn, conv_id: str, system_prompt: str | None):
    """Newest messages first until the context budget is spent."""
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conv_id=? ORDER BY id DESC",
        (conv_id,),
    ).fetchall()

    budget = MAX_CONTEXT_TOKENS
    picked = []
    for row in rows:  # walk backwards
        cost = estimate_tokens(row["content"])
        if cost > budget:
            break
        budget -= cost
        picked.append({"role": row["role"], "content": row["content"]})

    messages = list(reversed(picked))
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    return messages


def parse_key_entries(raw: str | None) -> list[dict]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        parsed = None

    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        items = [parsed]
    else:
        items = [
            {"key": part.strip(), "token_budget": 0, "used_tokens": 0}
            for part in re.split(r"[\r\n,]+", raw)
            if part.strip()
        ]

    result = []
    for item in items:
        if isinstance(item, str):
            key = item.strip()
            if key:
                result.append({"key": key, "token_budget": 0, "used_tokens": 0})
        elif isinstance(item, dict):
            key = str(item.get("key") or item.get("api_key") or "").strip()
            if not key:
                continue
            quota = item.get("token_budget") or item.get("quota") or 0
            used = item.get("used_tokens") or 0
            result.append(
                {
                    "key": key,
                    "token_budget": int(quota) if quota else 0,
                    "used_tokens": int(used) if used else 0,
                }
            )
    return result


def provider_key_entries(row) -> list[dict]:
    entries = all_provider_key_entries(row)
    return [
        e
        for e in entries
        if not (
            e.get("token_budget")
            and int(e.get("used_tokens") or 0) >= int(e.get("token_budget") or 0)
        )
    ]


def all_provider_key_entries(row) -> list[dict]:
    try:
        secret = decrypt_secret(row["api_key_enc"])
    except Exception:
        return []
    entries = parse_key_entries(secret)
    if entries:
        return entries
    plain = (row["api_key_enc"] or "").strip()
    if plain:
        return [{"key": plain, "token_budget": 0, "used_tokens": 0}]
    return []


def public_provider(row) -> dict:
    """What the browser is allowed to see: never the key itself."""
    entries = all_provider_key_entries(row)
    usage = ensure_daily_usage(row["id"], row["base_url"])
    try:
        masked = mask_key(entries[0]["key"]) if entries else ""
        key_ok = True
    except Exception:
        masked, key_ok = "", False
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "default_model": row["default_model"],
        "is_active": bool(row["is_active"]),
        "has_key": bool(row["api_key_enc"]),
        "key_masked": masked,
        "key_readable": key_ok,
        "key_count": len(entries),
        "token_budget": sum(int(e.get("token_budget") or 0) for e in entries),
        "daily_token_budget": usage["token_budget"],
        "daily_tokens_used": usage["tokens_used"],
        "daily_tokens_remaining": (
            max(0, usage["token_budget"] - usage["tokens_used"])
            if usage["token_budget"]
            else None
        ),
        "daily_usage_date": usage["utc_date"],
    }


def load_provider(conn, provider_id: str | None):
    """Provider row by id, falling back to the active one."""
    row = None
    if provider_id:
        row = conn.execute(
            "SELECT * FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM providers WHERE is_active=1").fetchone()
    if row is None:
        raise HTTPException(400, "No API provider configured. Add one in Providers.")
    return row


def provider_creds(row) -> tuple[str, str]:
    entries = provider_key_entries(row)
    if not entries:
        raise HTTPException(400, f"No valid API key stored for '{row['name']}'")
    return row["base_url"], entries[0]["key"]


def provider_keys(row) -> list[str]:
    entries = provider_key_entries(row)
    return [e["key"] for e in entries if e.get("key")]


def record_key_usage(provider_id: str, key: str, consumed_tokens: int):
    if consumed_tokens <= 0:
        return
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        if not row:
            return
        entries = all_provider_key_entries(row)
        for entry in entries:
            if (entry.get("key") or "") == key:
                entry["used_tokens"] = (
                    int(entry.get("used_tokens") or 0) + consumed_tokens
                )
                break
        payload = json.dumps(entries, separators=(",", ":")) if entries else None
        conn.execute(
            "UPDATE providers SET api_key_enc=? WHERE id=?",
            (encrypt_secret(payload) if payload else None, provider_id),
        )


def auth_headers(key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:  # local servers (Ollama, LM Studio) need none
        headers["Authorization"] = f"Bearer {key}"
    return headers


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(422, "API URL must start with http:// or https://")
    return url


def upstream_error(
    status: int, body: str, provider: str, retry_after: str | None = None
) -> str:
    """Turn provider responses into something a human can act on."""
    detail = body.strip()
    try:
        obj = json.loads(body)
        err = obj.get("error", obj) if isinstance(obj, dict) else obj
        if isinstance(err, dict):
            detail = err.get("message") or err.get("detail") or json.dumps(err)
        elif isinstance(err, str):
            detail = err
    except (json.JSONDecodeError, AttributeError):
        pass
    detail = str(detail)[:300]

    if status in (401, 403):
        head = f"{provider}: the API key was rejected (invalid, expired or no access)."
    elif status == 404:
        head = (
            f"{provider}: endpoint or model not found. Check the API URL and model id."
        )
    elif status == 429:
        wait = f" Retry after {retry_after}s." if retry_after else ""
        head = f"{provider}: rate limit or quota reached.{wait}"
    elif status >= 500:
        head = f"{provider}: the service is having problems (HTTP {status}). Try again later."
    else:
        head = f"{provider}: request rejected (HTTP {status})."
    return f"{head} {detail}".strip()


def network_error(exc: Exception, provider: str) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"{provider}: the request timed out. The service may be overloaded."
    if isinstance(exc, httpx.ConnectError):
        return f"{provider}: could not connect. Check the API URL or your network."
    return f"{provider}: network error: {exc}"


WORKSPACE_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    "graphify-out",
}
WORKSPACE_EXCLUDED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".dll",
    ".exe",
    ".bin",
}
EDIT_BLOCK_RE = re.compile(r"<OXION_EDITS>\s*(.*?)\s*</OXION_EDITS>", re.DOTALL)


def normalize_workspace_root(raw_root: str | None = None) -> Path:
    """Resolve a user-selected project directory without changing the app root."""
    if not raw_root:
        return WORKSPACE_ROOT.resolve()
    candidate = Path(raw_root).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(400, "Project path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, "Project folder does not exist") from exc
    if not resolved.is_dir():
        raise HTTPException(400, "Project path must be a directory")
    return resolved


def ensure_local_workspace_available() -> None:
    """Reject local project access when running in a hosted serverless runtime."""
    if IS_VERCEL:
        raise HTTPException(
            501,
            "Online project workspaces are not configured. Vercel cannot access "
            "folders on your computer; use Chat mode or run Coding locally.",
        )


def workspace_root_for_conversation(conv_id: str | None = None) -> Path:
    """Return the project selected for a Coding conversation."""
    ensure_local_workspace_available()
    if not conv_id:
        raise HTTPException(400, "Select a Coding conversation and open its project")
    with db() as conn:
        row = conn.execute(
            "SELECT project_root, workspace_type FROM conversations WHERE id=?",
            (conv_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Conversation not found")
    if row["workspace_type"] != "coding":
        raise HTTPException(409, "Project files are available only in Coding chats")
    if not row["project_root"]:
        raise HTTPException(400, "Open a project for this Coding chat first")
    return normalize_workspace_root(row["project_root"])


def enforce_rate_limit(conv_id: str) -> None:
    """Limit a conversation to 60 requests in a rolling one-minute window."""
    now = time.monotonic()
    with REQUEST_TIMES_LOCK:
        requests = REQUEST_TIMES[conv_id]
        while requests and now - requests[0] >= 60:
            requests.popleft()
        if len(requests) >= 60:
            raise HTTPException(429, "Rate limit exceeded; try again in a minute")
        requests.append(now)


def workspace_path(raw_path: str, project_root: Path | None = None) -> Path:
    project_root = (project_root or WORKSPACE_ROOT).resolve()
    raw_path = (raw_path or "").strip()
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(400, "File path must stay inside the project folder")
    if any(part.lower() in WORKSPACE_EXCLUDED_DIRS for part in relative.parts):
        raise HTTPException(404, "File is excluded from workspace access")
    name = relative.name.lower()
    if (
        name == ".env"
        or name.startswith(".env.")
        or name
        in {
            "credentials",
            "credentials.json",
            "secrets",
            "secrets.json",
            "id_rsa",
            "id_ed25519",
        }
        or name.endswith(tuple(WORKSPACE_EXCLUDED_SUFFIXES))
    ):
        raise HTTPException(404, "Sensitive or binary files are excluded")
    candidate = (project_root / relative).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        raise HTTPException(400, "File path must stay inside the project folder")
    return candidate


def workspace_candidates(project_root: Path | None = None) -> list[Path]:
    project_root = (project_root or WORKSPACE_ROOT).resolve()
    candidates = []
    for current, directories, filenames in os.walk(project_root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if name.lower() not in WORKSPACE_EXCLUDED_DIRS
            and not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            relative = (current_path / filename).relative_to(project_root)
            try:
                candidate = workspace_path(relative.as_posix(), project_root)
                if (
                    candidate.is_file()
                    and candidate.stat().st_size <= MAX_WORKSPACE_FILE_BYTES
                ):
                    candidates.append(candidate)
            except (HTTPException, OSError):
                continue
    return sorted(
        candidates,
        key=lambda path: path.relative_to(project_root).as_posix().lower(),
    )


def read_workspace_text(path: Path) -> str:
    if path.stat().st_size > MAX_WORKSPACE_FILE_BYTES:
        raise HTTPException(413, "File is too large to read in Coding")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            415, "Binary or non-UTF-8 files are not available in Coding"
        )


def workspace_snapshot(project_root: Path | None = None) -> dict:
    project_root = (project_root or WORKSPACE_ROOT).resolve()
    files: list[dict[str, Any]] = []
    context_parts: list[str] = []
    used_chars = 0
    omitted = 0
    for path in workspace_candidates(project_root):
        if len(files) >= MAX_WORKSPACE_FILES:
            omitted += 1
            continue
        try:
            content = read_workspace_text(path)
        except HTTPException:
            omitted += 1
            continue
        relative = path.relative_to(project_root).as_posix()
        block = f"\n--- FILE: {relative} ---\n{content}"
        if used_chars + len(block) > MAX_WORKSPACE_CONTEXT_CHARS:
            omitted += 1
            continue
        used_chars += len(block)
        files.append(
            {
                "path": relative,
                "content": content,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "size": len(content.encode("utf-8")),
            }
        )
        context_parts.append(block)
    return {
        "files": files,
        "omitted_count": omitted,
        "context": "\n".join(context_parts),
    }


def extract_edit_proposal(reply: str) -> tuple[str, list[dict] | None]:
    match = EDIT_BLOCK_RE.search(reply)
    if not match:
        return reply, None
    visible_reply = (reply[: match.start()] + reply[match.end() :]).strip()
    try:
        proposal = json.loads(match.group(1))
    except json.JSONDecodeError:
        return visible_reply, None
    changes = proposal.get("changes") if isinstance(proposal, dict) else None
    if not isinstance(changes, list) or not 1 <= len(changes) <= 10:
        return visible_reply, None
    if any(
        not isinstance(change, dict)
        or not isinstance(change.get("path"), str)
        or not isinstance(change.get("content"), str)
        for change in changes
    ):
        return visible_reply, None
    return visible_reply, changes


def stage_workspace_edits(
    changes: list[dict],
    project_root: Path | None = None,
    conv_id: str | None = None,
) -> dict:
    if not 1 <= len(changes) <= 10:
        raise HTTPException(422, "Choose between 1 and 10 files to stage")
    project_root = (project_root or WORKSPACE_ROOT).resolve()
    staged = []
    seen = set()
    for change in changes:
        relative = str(change["path"]).replace("\\", "/")
        path = workspace_path(relative, project_root)
        normalized = path.relative_to(project_root).as_posix()
        if normalized in seen:
            raise HTTPException(422, f"Duplicate file in proposal: {normalized}")
        seen.add(normalized)
        content = change["content"]
        if len(content.encode("utf-8")) > MAX_WORKSPACE_FILE_BYTES:
            raise HTTPException(413, f"Proposed file is too large: {normalized}")
        if path.exists():
            old_content = read_workspace_text(path)
            expected_sha256 = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
        else:
            if not path.parent.is_dir():
                raise HTTPException(400, f"Parent folder does not exist: {normalized}")
            old_content = None
            expected_sha256 = None
        staged.append(
            {
                "path": normalized,
                "content": content,
                "old_content": old_content,
                "expected_sha256": expected_sha256,
            }
        )
    review_id = uuid.uuid4().hex
    PENDING_WORKSPACE_EDITS[review_id] = {
        "created_at": time.time(),
        "changes": staged,
        "project_root": str(project_root),
        "conv_id": conv_id,
    }
    return {"review_id": review_id, "changes": staged}


def apply_staged_workspace_edits(
    review_id: str,
) -> dict[str, list[str] | list[int]]:
    staged = PENDING_WORKSPACE_EDITS.get(review_id)
    if not staged or time.time() - staged["created_at"] > 900:
        PENDING_WORKSPACE_EDITS.pop(review_id, None)
        raise HTTPException(
            404, "This edit review has expired. Ask Coding to propose it again."
        )
    project_root = normalize_workspace_root(staged["project_root"])
    changes = staged["changes"]
    resolved = []
    for change in changes:
        path = workspace_path(change["path"], project_root)
        current_hash = None
        if path.exists():
            current = read_workspace_text(path)
            current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if current_hash != change["expected_sha256"]:
            raise HTTPException(
                409,
                f"{change['path']} changed after review. Generate a fresh proposal.",
            )
        resolved.append((path, change["content"]))

    temporary_paths = []
    try:
        for path, content in resolved:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(content)
                temporary_paths.append((Path(temp_file.name), path))
        for temporary, target in temporary_paths:
            os.replace(temporary, target)
    finally:
        for temporary, _ in temporary_paths:
            if temporary.exists():
                temporary.unlink()
    edit_ids = []
    conv_id = staged.get("conv_id")
    if conv_id:
        with db() as conn:
            for change in changes:
                cursor = conn.execute(
                    "INSERT INTO workspace_edits"
                    "(conv_id,file_path,project_root,old_content,new_content,applied_at) "
                    "VALUES(?,?,?,?,?,?) RETURNING id",
                    (
                        conv_id,
                        change["path"],
                        str(project_root),
                        change["old_content"],
                        change["content"],
                        time.time(),
                    ),
                )
                edit_ids.append(int(cursor.fetchone()["id"]))
    del PENDING_WORKSPACE_EDITS[review_id]
    return {
        "files": [path.relative_to(project_root).as_posix() for path, _ in resolved],
        "edit_ids": edit_ids,
    }


def model_ids(data) -> list[str]:
    """OpenAI returns {data:[{id}]}; others use {models:[...]} or a bare list."""
    items = data
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    ids = []
    for m in items if isinstance(items, list) else []:
        if isinstance(m, str):
            ids.append(m)
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                ids.append(str(mid))
    return sorted(set(ids))


async def fetch_models(base_url: str, key: str, name: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{base_url}/models", headers=auth_headers(key))
    except httpx.HTTPError as exc:
        raise HTTPException(502, network_error(exc, name))
    if r.status_code != 200:
        status = r.status_code if r.status_code in (401, 403, 404, 429) else 502
        raise HTTPException(
            status,
            upstream_error(r.status_code, r.text, name, r.headers.get("retry-after")),
        )
    try:
        return model_ids(r.json())
    except json.JSONDecodeError:
        raise HTTPException(
            502,
            f"{name}: /models did not return JSON. "
            "Is this an OpenAI-compatible API URL?",
        )


# ---------------------------------------------------------------- schemas
class NewConversation(BaseModel):
    model: str = "gemma-2-2b"
    provider_id: str | None = None
    project_root: str | None = None
    workspace_type: str = "chat"


class WorkspaceTypeInput(BaseModel):
    workspace_type: str


class ChatIn(BaseModel):
    conv_id: str
    content: str
    model: str
    provider_id: str | None = None
    system_prompt: str | None = None
    coding_mode: bool = False
    skill_name: str | None = None


class SkillExecute(BaseModel):
    skill_name: str
    task: str
    conv_id: str | None = None


class WorkspaceProposal(BaseModel):
    changes: list[dict]
    conv_id: str | None = None


class WorkspaceApproval(BaseModel):
    approved: bool


class ProjectSelection(BaseModel):
    path: str


class CommandIn(BaseModel):
    command: str
    approved: bool = False


class ProviderIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    api_keys: str | None = None
    token_budget: int | None = None
    default_model: str | None = None


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # None/"" = keep the saved key
    api_keys: str | None = None
    token_budget: int | None = None
    default_model: str | None = None
    clear_key: bool = False


class ProviderTest(BaseModel):
    base_url: str
    api_key: str = ""
    api_keys: str | None = None
    provider_id: str | None = None  # reuse the saved key when api_key is blank


# ---------------------------------------------------------------- static
@app.get("/")
async def index():
    return FileResponse(WORKSPACE_ROOT / "static" / "index.html")


@app.get("/api/capabilities")
async def capabilities():
    """Expose deployment-specific features to the client without secrets."""
    return {"hosted": IS_VERCEL, "coding_workspaces": not IS_VERCEL}


def public_skill(skill: BaseSkill) -> dict[str, Any]:
    """Serialize a skill without exposing implementation details."""
    data = skill.config.as_dict()
    data["prompt"] = skill.prompt
    return data


@app.get("/api/skills")
@app.get("/api/v1/skills")
@app.post("/api/v1/skills")
async def list_skills():
    """List discovered skills and their activation state."""
    return [
        public_skill(SKILL_REGISTRY.get_skill(config.name))
        for config in SKILL_REGISTRY.list_skills()
    ]


@app.post("/api/skills/{name}/activate")
@app.post("/api/v1/skills/{name}/activate")
async def activate_skill(name: str):
    """Enable and persist one discovered skill."""
    try:
        SKILL_REGISTRY.activate_skill(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "name": name, "enabled": True}


@app.post("/api/skills/{name}/deactivate")
@app.post("/api/v1/skills/{name}/deactivate")
async def deactivate_skill(name: str):
    """Disable and persist one discovered skill."""
    try:
        SKILL_REGISTRY.deactivate_skill(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "name": name, "enabled": False}


@app.post("/api/skills/execute")
@app.post("/api/v1/skills/execute")
async def execute_skill(body: SkillExecute):
    """Prepare a task with one enabled skill's prompt and safe context."""
    context: dict[str, Any] = {}
    if body.conv_id:
        with db() as conn:
            row = conn.execute(
                "SELECT workspace_type FROM conversations WHERE id=?",
                (body.conv_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Conversation not found")
        context["workspace_type"] = row["workspace_type"]
        if row["workspace_type"] == "coding":
            context["project_root"] = str(workspace_root_for_conversation(body.conv_id))
    try:
        result = SKILL_REGISTRY.execute_skill(body.skill_name, body.task, context)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"skill": body.skill_name, "prepared_prompt": result}


@app.get("/api/workspace/tree")
async def workspace_tree(conv_id: str | None = None):
    project_root = workspace_root_for_conversation(conv_id)
    if conv_id:
        enforce_rate_limit(conv_id)
    snapshot = workspace_snapshot(project_root)
    return {
        "root": project_root.name,
        "project_root": str(project_root),
        "files": [
            {"path": file["path"], "size": file["size"]} for file in snapshot["files"]
        ],
        "omitted_count": snapshot["omitted_count"],
    }


@app.get("/api/workspace/file")
async def workspace_file(path: str, conv_id: str | None = None):
    project_root = workspace_root_for_conversation(conv_id)
    if conv_id:
        enforce_rate_limit(conv_id)
    resolved = workspace_path(path, project_root)
    if not resolved.is_file():
        raise HTTPException(404, "File not found")
    content = read_workspace_text(resolved)
    return {
        "path": resolved.relative_to(project_root).as_posix(),
        "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


@app.post("/api/workspace/preview")
async def workspace_preview(body: WorkspaceProposal):
    """Stage proposed changes against the selected conversation's project."""
    project_root = workspace_root_for_conversation(body.conv_id)
    if body.conv_id:
        enforce_rate_limit(body.conv_id)
    return stage_workspace_edits(body.changes, project_root, body.conv_id)


@app.post("/api/workspace/{review_id}/apply")
async def workspace_apply(review_id: str, body: WorkspaceApproval):
    """Apply a still-current workspace proposal after explicit approval."""
    if not body.approved:
        raise HTTPException(400, "Explicit approval is required")
    staged = PENDING_WORKSPACE_EDITS.get(review_id)
    if staged and staged.get("conv_id"):
        enforce_rate_limit(staged["conv_id"])
    result = apply_staged_workspace_edits(review_id)
    return {"ok": True, **result}


def parse_safe_command(command: str) -> list[str]:
    """Parse a command and accept only reviewed, non-shell command prefixes."""
    if not command or len(command) > MAX_COMMAND_LENGTH:
        raise HTTPException(
            422, f"Command must be between 1 and {MAX_COMMAND_LENGTH} characters"
        )
    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise HTTPException(422, "Command syntax is invalid") from exc
    args = [
        (
            arg[1:-1]
            if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in {"'", '"'}
            else arg
        )
        for arg in args
    ]
    normalized = [arg.lower() for arg in args]
    allowed = any(
        len(normalized) >= len(prefix)
        and tuple(normalized[: len(prefix)]) == tuple(part.lower() for part in prefix)
        for prefix in SAFE_COMMANDS
    )
    if not allowed:
        raise HTTPException(403, "Command is not on the safe-command allowlist")
    if normalized[0] == "git" and any(arg.startswith("-") for arg in normalized[2:]):
        raise HTTPException(403, "Git command options are not allowed")
    if normalized[:2] == ["ruff", "check"] and any(
        arg in {"--fix", "--fix-only", "--unsafe-fixes"}
        or arg.startswith("--output-file")
        for arg in normalized[2:]
    ):
        raise HTTPException(403, "File-modifying Ruff options are not allowed")
    return args


def command_worker(job_id: str, args: list[str], project_root: Path) -> None:
    """Run one allowlisted command and retain its bounded output."""
    with COMMAND_JOBS_LOCK:
        COMMAND_JOBS[job_id]["status"] = "running"
    try:
        executable = args[0].lower()
        if executable == "python":
            args[0] = sys.executable
        elif executable == "pytest":
            args = [sys.executable, "-m", "pytest", *args[1:]]
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            (str(Path(sys.executable).parent), environment.get("PATH", ""))
        )
        process = subprocess.Popen(
            args,
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=environment,
        )
        try:
            output, _ = process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
            status = "completed"
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            output = (
                f"{output}\nCommand timed out after {COMMAND_TIMEOUT_SECONDS} seconds"
            )
            status = "failed"
        with COMMAND_JOBS_LOCK:
            COMMAND_JOBS[job_id].update(
                status=status,
                exit_code=process.returncode,
                output=output[-MAX_COMMAND_OUTPUT:],
            )
    except OSError as exc:
        with COMMAND_JOBS_LOCK:
            COMMAND_JOBS[job_id].update(
                status="failed", exit_code=None, output=str(exc)
            )


@app.post("/api/conversations/{conv_id}/commands")
async def execute_command(conv_id: str, body: CommandIn):
    """Queue an allowlisted command after explicit user approval."""
    if not body.approved:
        raise HTTPException(400, "Explicit approval is required")
    project_root = workspace_root_for_conversation(conv_id)
    enforce_rate_limit(conv_id)
    args = parse_safe_command(body.command)
    job_id = uuid.uuid4().hex
    with COMMAND_JOBS_LOCK:
        COMMAND_JOBS[job_id] = {
            "conv_id": conv_id,
            "status": "queued",
            "exit_code": None,
            "output": "",
        }
    worker = threading.Thread(
        target=command_worker,
        args=(job_id, args, project_root),
        daemon=True,
    )
    worker.start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/commands/{job_id}")
async def command_status(job_id: str, conv_id: str):
    """Return one conversation's command-job state and bounded output."""
    with COMMAND_JOBS_LOCK:
        job = COMMAND_JOBS.get(job_id)
        if not job or job["conv_id"] != conv_id:
            raise HTTPException(404, "Command job not found")
        status = {key: value for key, value in job.items() if key != "conv_id"}
    workspace_root_for_conversation(conv_id)
    enforce_rate_limit(conv_id)
    return status


@app.post("/api/conversations/{conv_id}/project")
async def select_conversation_project(conv_id: str, body: ProjectSelection):
    """Set the local project directory associated with one conversation."""
    if IS_VERCEL:
        raise HTTPException(
            501,
            "Online project workspaces are not configured. Vercel cannot access "
            "folders on your computer; use Chat mode or run Coding locally.",
        )
    project_root = normalize_workspace_root(body.path)
    with db() as conn:
        exists = conn.execute(
            "SELECT workspace_type FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Conversation not found")
        if exists["workspace_type"] != "coding":
            raise HTTPException(409, "Projects can be opened only in Coding chats")
        enforce_rate_limit(conv_id)
        cursor = conn.execute(
            "UPDATE conversations SET project_root=?, updated_at=? WHERE id=?",
            (str(project_root), time.time(), conv_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Conversation not found")
    return {"project_root": str(project_root), "root": project_root.name}


# ---------------------------------------------------------------- providers
@app.get("/api/providers")
async def list_providers():
    with db() as conn:
        rows = conn.execute("SELECT * FROM providers ORDER BY created_at").fetchall()
    return [public_provider(r) for r in rows]


@app.post("/api/providers")
async def create_provider(body: ProviderIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Name is required")
    base_url = normalize_base_url(body.base_url)
    keys = []
    if body.api_keys:
        keys = parse_key_entries(body.api_keys)
    elif body.api_key:
        keys = parse_key_entries(body.api_key)
    payload = json.dumps(keys, separators=(",", ":")) if keys else None
    pid = uuid.uuid4().hex[:12]
    now = time.time()
    with db() as conn:
        first = not conn.execute("SELECT 1 FROM providers").fetchone()
        conn.execute(
            "INSERT INTO providers(id,name,base_url,api_key_enc,default_model,"
            "is_active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                pid,
                name,
                base_url,
                encrypt_secret(payload) if payload else None,
                (body.default_model or "").strip() or None,
                1 if first else 0,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    return public_provider(row)


@app.patch("/api/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderPatch):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Provider not found")
        name = body.name.strip() if body.name is not None else row["name"]
        if not name:
            raise HTTPException(422, "Name is required")
        base_url = (
            normalize_base_url(body.base_url)
            if body.base_url is not None
            else row["base_url"]
        )
        key_enc = row["api_key_enc"]
        if body.clear_key:
            key_enc = None
        elif body.api_key and body.api_key.strip():
            key_enc = encrypt_secret(
                json.dumps(parse_key_entries(body.api_key), separators=(",", ":"))
            )
        elif body.api_keys is not None:
            items = parse_key_entries(body.api_keys)
            key_enc = (
                encrypt_secret(json.dumps(items, separators=(",", ":")))
                if items
                else None
            )
        default_model = (
            body.default_model.strip() or None
            if body.default_model is not None
            else row["default_model"]
        )
        conn.execute(
            "UPDATE providers SET name=?, base_url=?, api_key_enc=?,"
            " default_model=?, updated_at=? WHERE id=?",
            (name, base_url, key_enc, default_model, time.time(), provider_id),
        )
        row = conn.execute(
            "SELECT * FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
    return public_provider(row)


@app.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT is_active FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Provider not found")
        conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))
        # orphaned chats fall back to whichever provider is active
        conn.execute(
            "UPDATE conversations SET provider_id=NULL WHERE provider_id=?",
            (provider_id,),
        )
        if row["is_active"]:
            nxt = conn.execute(
                "SELECT id FROM providers ORDER BY created_at LIMIT 1"
            ).fetchone()
            if nxt:
                conn.execute(
                    "UPDATE providers SET is_active=1 WHERE id=?", (nxt["id"],)
                )
    return {"ok": True}


@app.post("/api/providers/{provider_id}/activate")
async def activate_provider(provider_id: str):
    with db() as conn:
        if not conn.execute(
            "SELECT 1 FROM providers WHERE id=?", (provider_id,)
        ).fetchone():
            raise HTTPException(404, "Provider not found")
        conn.execute("UPDATE providers SET is_active=0")
        conn.execute("UPDATE providers SET is_active=1 WHERE id=?", (provider_id,))
    return {"ok": True}


@app.get("/api/providers/{provider_id}/models")
async def provider_models(provider_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Provider not found")
    base_url, key = provider_creds(row)
    return {"models": await fetch_models(base_url, key, row["name"])}


@app.get("/api/providers/{provider_id}/usage")
async def provider_usage(provider_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT id, base_url FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Provider not found")
        usage = daily_usage_in_conn(
            conn, daily_usage_id(row["id"], row["base_url"]), row["base_url"]
        )
    budget = usage["token_budget"]
    return {
        **usage,
        "tokens_remaining": max(0, budget - usage["tokens_used"]) if budget else None,
        "reset_at": "00:00 UTC",
    }


@app.post("/api/providers/test")
async def test_provider(body: ProviderTest):
    """Check URL + key before saving, by listing models."""
    base_url = normalize_base_url(body.base_url)
    keys = parse_key_entries(body.api_keys or body.api_key)
    name = "Provider"
    if not keys and body.provider_id:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM providers WHERE id=?", (body.provider_id,)
            ).fetchone()
        if row:
            name = row["name"]
            keys = [
                {"key": k, "token_budget": 0, "used_tokens": 0}
                for k in provider_keys(row)
            ]
    if not keys:
        raise HTTPException(422, "At least one API key is required")
    for item in keys:
        try:
            models = await fetch_models(base_url, item["key"], name)
            return {"ok": True, "models": models}
        except HTTPException:
            continue
    raise HTTPException(
        401, f"{name}: no valid API keys were accepted for this provider"
    )


# ---------------------------------------------------------------- models
@app.get("/api/models")
async def list_models():
    """Models of the active provider (kept for older clients)."""
    with db() as conn:
        row = load_provider(conn, None)
    base_url, key = provider_creds(row)
    return {"data": [{"id": m} for m in await fetch_models(base_url, key, row["name"])]}


# ---------------------------------------------------------------- conversations
@app.get("/api/conversations")
async def list_conversations(workspace_type: str | None = None):
    """List conversations, optionally filtered to Chat or Coding."""
    if workspace_type not in {None, "all", "chat", "coding"}:
        raise HTTPException(422, "workspace_type must be chat, coding, or all")
    query = """SELECT c.id, c.title, c.model, c.provider_id, c.project_root,
                      c.workspace_type, c.updated_at,
                      p.name AS provider_name,
                      (SELECT COALESCE(SUM(tokens),0) FROM messages m
                        WHERE m.conv_id = c.id) AS tokens,
                      (SELECT COALESCE(SUM(tokens),0) FROM messages m
                        WHERE m.conv_id = c.id AND m.role='user') AS input_tokens,
                      (SELECT COALESCE(SUM(tokens),0) FROM messages m
                        WHERE m.conv_id = c.id AND m.role='assistant') AS output_tokens
               FROM conversations c
               LEFT JOIN providers p ON p.id = c.provider_id"""
    parameters: tuple[str, ...] = ()
    if workspace_type not in {None, "all"}:
        query += "\n               WHERE c.workspace_type=?"
        parameters = (workspace_type,)
    query += "\n               ORDER BY c.updated_at DESC"
    with db() as conn:
        rows = conn.execute(query, parameters).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/conversations")
async def create_conversation(body: NewConversation):
    if body.workspace_type not in {"chat", "coding"}:
        raise HTTPException(422, "workspace_type must be chat or coding")
    if body.workspace_type == "coding":
        ensure_local_workspace_available()
    conv_id = uuid.uuid4().hex[:12]
    now = time.time()
    project_root = (
        str(normalize_workspace_root(body.project_root))
        if body.workspace_type == "coding" and body.project_root
        else None
    )
    with db() as conn:
        provider_id = body.provider_id
        if not provider_id:
            active = conn.execute(
                "SELECT id FROM providers WHERE is_active=1"
            ).fetchone()
            provider_id = active["id"] if active else None
        conn.execute(
            "INSERT INTO conversations"
            "(id,title,model,provider_id,created_at,updated_at,project_root,"
            "workspace_type) VALUES(?,?,?,?,?,?,?,?)",
            (
                conv_id,
                "New chat",
                body.model,
                provider_id,
                now,
                now,
                project_root,
                body.workspace_type,
            ),
        )
    return {
        "id": conv_id,
        "title": "New chat",
        "model": body.model,
        "provider_id": provider_id,
        "project_root": project_root,
        "workspace_type": body.workspace_type,
    }


@app.post("/api/conversations/{conv_id}/workspace")
async def update_conversation_workspace(conv_id: str, body: WorkspaceTypeInput):
    """Move a conversation between the separate Chat and Coding lists."""
    if body.workspace_type not in {"chat", "coding"}:
        raise HTTPException(422, "workspace_type must be chat or coding")
    if body.workspace_type == "coding":
        ensure_local_workspace_available()
    with db() as conn:
        row = conn.execute(
            "SELECT project_root FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Conversation not found")
        enforce_rate_limit(conv_id)
        project_root = row["project_root"] if body.workspace_type == "coding" else None
        conn.execute(
            "UPDATE conversations SET workspace_type=?,project_root=?,updated_at=? "
            "WHERE id=?",
            (body.workspace_type, project_root, time.time(), conv_id),
        )
    return {
        "workspace_type": body.workspace_type,
        "project_root": project_root,
    }


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    """Return one conversation and its persisted messages."""
    with db() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if not conv:
            raise HTTPException(404, "Conversation not found")
        enforce_rate_limit(conv_id)
        msgs = conn.execute(
            "SELECT role, content, tokens FROM messages" " WHERE conv_id=? ORDER BY id",
            (conv_id,),
        ).fetchall()
    return {"conversation": dict(conv), "messages": [dict(m) for m in msgs]}


@app.get("/api/conversations/{conv_id}/edits")
async def conversation_edits(conv_id: str):
    """List recent approved workspace edits for one conversation."""
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Conversation not found")
        enforce_rate_limit(conv_id)
        rows = conn.execute(
            "SELECT id,file_path,applied_at,undone_at FROM workspace_edits "
            "WHERE conv_id=? ORDER BY id DESC LIMIT 100",
            (conv_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/conversations/{conv_id}/edits/{edit_id}/undo")
async def undo_conversation_edit(conv_id: str, edit_id: int, body: WorkspaceApproval):
    """Restore one edit only when the file still matches the applied version."""
    if not body.approved:
        raise HTTPException(400, "Explicit approval is required to undo an edit")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM workspace_edits WHERE id=? AND conv_id=?",
            (edit_id, conv_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Edit history entry not found")
    enforce_rate_limit(conv_id)
    if row["undone_at"] is not None:
        raise HTTPException(409, "This edit has already been undone")
    project_root = normalize_workspace_root(row["project_root"])
    path = workspace_path(row["file_path"], project_root)
    if not path.is_file():
        raise HTTPException(409, "File changed since the edit; undo was not applied")
    current = read_workspace_text(path)
    if (
        hashlib.sha256(current.encode("utf-8")).hexdigest()
        != hashlib.sha256(row["new_content"].encode("utf-8")).hexdigest()
    ):
        raise HTTPException(409, "File changed since the edit; undo was not applied")
    if row["old_content"] is None:
        path.unlink()
    else:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(row["old_content"])
            temporary = Path(temp_file.name)
        try:
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    with db() as conn:
        conn.execute(
            "UPDATE workspace_edits SET undone_at=? WHERE id=? AND conv_id=?",
            (time.time(), edit_id, conv_id),
        )
    return {"ok": True, "file": row["file_path"]}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    """Delete one conversation and its related messages and edits."""
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Conversation not found")
        enforce_rate_limit(conv_id)
        conn.execute("DELETE FROM workspace_edits WHERE conv_id=?", (conv_id,))
        conn.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    with REQUEST_TIMES_LOCK:
        REQUEST_TIMES.pop(conv_id, None)
    for review_id, proposal in list(PENDING_WORKSPACE_EDITS.items()):
        if proposal.get("conv_id") == conv_id:
            PENDING_WORKSPACE_EDITS.pop(review_id, None)
    with COMMAND_JOBS_LOCK:
        for job_id, job in list(COMMAND_JOBS.items()):
            if job["conv_id"] == conv_id and job["status"] in {"completed", "failed"}:
                COMMAND_JOBS.pop(job_id, None)
    return {"ok": True}


# ---------------------------------------------------------------- chat
@app.post("/api/chat")
async def chat(body: ChatIn):
    with db() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id=?", (body.conv_id,)
        ).fetchone()
        if not conv:
            raise HTTPException(404, "Conversation not found")
        enforce_rate_limit(body.conv_id)
        is_coding_chat = conv["workspace_type"] == "coding"
        if is_coding_chat and not conv["project_root"]:
            raise HTTPException(400, "Open a project for this Coding chat first")
        project_root = (
            normalize_workspace_root(conv["project_root"]) if is_coding_chat else None
        )

        # explicit choice > what the chat used last > active provider
        provider = load_provider(conn, body.provider_id or conv["provider_id"])
        base_url = provider["base_url"]
        api_keys = provider_keys(provider)
        if not api_keys:
            raise HTTPException(
                400, f"No API keys available for provider '{provider['name']}'"
            )
        provider_name = provider["name"]
        daily_usage = daily_usage_in_conn(
            conn, daily_usage_id(provider["id"], base_url), base_url
        )
        daily_budget = daily_usage["token_budget"]
        if daily_budget and daily_usage["tokens_used"] >= daily_budget:
            raise HTTPException(
                429,
                f"Daily token limit reached ({daily_budget:,}). It resets at 00:00 UTC.",
            )

        # persist the user turn first (removed again if the reply fails)
        now = time.time()
        user_msg_id = conn.execute(
            "INSERT INTO messages(conv_id,role,content,tokens,created_at)"
            " VALUES(?,?,?,?,?) RETURNING id",
            (body.conv_id, "user", body.content, estimate_tokens(body.content), now),
        ).fetchone()["id"]
        # first message becomes the title
        stored = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE conv_id=?", (body.conv_id,)
        ).fetchone()["c"]
        if stored == 1:
            conn.execute(
                "UPDATE conversations SET title=? WHERE id=?",
                (body.content.strip().replace("\n", " ")[:60], body.conv_id),
            )
        # the chat remembers the provider/model it last talked to
        conn.execute(
            "UPDATE conversations SET model=?, provider_id=?, updated_at=?"
            " WHERE id=?",
            (body.model, provider["id"], now, body.conv_id),
        )
        system_prompt = body.system_prompt or ""
        try:
            matched_skills = SKILL_REGISTRY.match_skills(body.content, body.skill_name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        selected_skill = matched_skills[0] if matched_skills else None
        if selected_skill:
            logger.info(
                "Selected skill %s for conversation %s",
                selected_skill.config.name,
                body.conv_id,
            )
            system_prompt += (
                "\n\n"
                + selected_skill.prompt
                + "\nAvailable capabilities: "
                + ", ".join(selected_skill.config.tools)
                + ". Use the existing approval workflow for file changes and "
                "commands; do not claim a tool was used unless it was."
            )
        if is_coding_chat:
            snapshot = workspace_snapshot(project_root)
            system_prompt += (
                "\n\nYou are working in the local project workspace. The text files below "
                "are the current project snapshot. Use only these files and paths. "
                "Never claim to have edited files or run commands. If the user asks for "
                "changes, propose them only; do not treat them as applied. After your "
                "normal explanation, append exactly one plain-text block in this format, "
                'without Markdown fences:\n<OXION_EDITS>{"changes":[{"path":"relative/path",'
                '"content":"complete replacement file text"}]}</OXION_EDITS>\n'
                "Include only changed files, use complete file contents, and escape JSON "
                "strings correctly. Omit the block if no file changes are needed. The user "
                "must review and approve every proposal before it is written.\n"
                f"Project files included: {len(snapshot['files'])}; omitted due to size/count: "
                f"{snapshot['omitted_count']}.\n{snapshot['context']}"
            )
        payload_messages = build_context(
            conn, body.conv_id, system_prompt.strip() or None
        )

    def fail(error: str) -> str:
        """Forget the unanswered user turn so a retry doesn't duplicate it."""
        with db() as conn:
            conn.execute("DELETE FROM messages WHERE id=?", (user_msg_id,))
        return f"data: {json.dumps({'error': error})}\n\n"

    async def event_stream():
        collected: list[str] = []
        usage = None
        last_error = ""
        used_key = api_keys[0] if api_keys else ""

        payload = {
            "model": body.model,
            "messages": payload_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300, connect=20)
            ) as client:
                for key_index, api_key in enumerate(api_keys):
                    used_key = api_key
                    for attempt in range(2):
                        try:
                            async with client.stream(
                                "POST",
                                f"{base_url}/chat/completions",
                                headers=auth_headers(api_key),
                                json=payload,
                            ) as resp:
                                if resp.status_code >= 400:
                                    detail = (await resp.aread()).decode(
                                        errors="replace"
                                    )
                                    if (
                                        attempt == 0
                                        and resp.status_code in (400, 422)
                                        and "stream_options" in detail
                                    ):
                                        payload.pop("stream_options", None)
                                        continue
                                    last_error = upstream_error(
                                        resp.status_code,
                                        detail,
                                        provider_name,
                                        resp.headers.get("retry-after"),
                                    )
                                    if (
                                        resp.status_code in (401, 403)
                                        and key_index < len(api_keys) - 1
                                    ):
                                        continue
                                    yield fail(last_error)
                                    return

                                async for line in resp.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    data = line[5:].strip()
                                    if data == "[DONE]":
                                        break
                                    try:
                                        obj = json.loads(data)
                                    except json.JSONDecodeError:
                                        continue

                                    if obj.get("usage"):
                                        usage = obj["usage"]

                                    choices = obj.get("choices") or []
                                    if choices:
                                        piece = (choices[0].get("delta") or {}).get(
                                            "content"
                                        )
                                        if piece:
                                            collected.append(piece)
                                            yield f"data: {json.dumps({'delta': piece})}\n\n"
                                    elif obj.get("error"):
                                        err = obj["error"]
                                        msg = (
                                            err.get("message")
                                            if isinstance(err, dict)
                                            else err
                                        )
                                        last_error = (
                                            f"{provider_name}: {str(msg)[:300]}"
                                        )
                                        if key_index < len(api_keys) - 1:
                                            break
                                        yield fail(last_error)
                                        return
                                break
                        except httpx.HTTPError as exc:
                            last_error = network_error(exc, provider_name)
                            if key_index < len(api_keys) - 1:
                                continue
                            yield fail(last_error)
                            return
                    if collected:
                        break
                    if key_index < len(api_keys) - 1 and last_error:
                        continue
                    if not collected and last_error:
                        yield fail(last_error)
                        return
                    break
        except httpx.HTTPError as exc:
            yield fail(network_error(exc, provider_name))
            return

        reply = "".join(collected)
        if not reply.strip():
            yield fail(
                last_error or f"{provider_name}: the model returned an empty reply."
            )
            return
        reply, proposed_edits = extract_edit_proposal(reply)
        if usage:
            tokens = int(usage.get("completion_tokens") or 0)
            total = int(usage.get("total_tokens") or 0)
        else:
            tokens = estimate_tokens(reply)
            total = None

        consumed_tokens = (
            total
            or sum(
                estimate_tokens(message["content"])
                for message in payload_messages
                if isinstance(message.get("content"), str)
            )
            + tokens
        )
        record_key_usage(provider["id"], used_key, consumed_tokens)
        record_daily_usage(provider["id"], base_url, consumed_tokens)

        with db() as conn:
            conn.execute(
                "INSERT INTO messages(conv_id,role,content,tokens,created_at)"
                " VALUES(?,?,?,?,?)",
                (body.conv_id, "assistant", reply, tokens, time.time()),
            )
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (time.time(), body.conv_id),
            )

        completion = {"done": True, "tokens": tokens, "usage": usage, "total": total}
        if selected_skill:
            completion["skill"] = {
                "name": selected_skill.config.name,
                "description": selected_skill.config.description,
            }
        if proposed_edits:
            completion["proposed_edits"] = proposed_edits
        yield f"data: {json.dumps(completion)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
