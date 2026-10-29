# Project Audit

## Scope and method

This is a read-only audit of the existing My Only Chat project. The live
application, provider configuration, and SQLite data were not modified. The
provider-key disclosure finding was reproduced using a temporary database and
a mocked upstream model-list request.

The root project is a single-process FastAPI application in `app.py`, a
vanilla-JavaScript frontend in `static/index.html`, and SQLite persistence in
`chat.db`. Provider keys are encrypted at rest (Windows DPAPI on Windows, or
Fernet on other platforms). The root has no Git metadata, so a repository diff
or a trustworthy uncommitted-change baseline is unavailable.

The nested `oxion-chat\` directory contains no first-party source files; it
contains only its virtual environment. `.claude-flow\` and `.swarm\` are local
generated state and were not inspected or modified. `tmp_rot.db` was present
and left untouched.

## Current project structure

First-party files observed at the audit snapshot:

- `app.py`
- `static\index.html`
- `tests\test_existing_app.py`
- `README.md`, `CHANGELOG.md`
- `requirements.txt`, `pyproject.toml`, `Makefile`
- `run.cmd`, `scripts\start.sh`
- `.env.example`, `.gitignore`, `.vscode\settings.json`
- `chat.db`

There was no root `.env` file and no existing `docs\` directory before this
report was created.

## Existing capabilities

- Multiple OpenAI-compatible providers and keys, per-conversation provider
  selection, encrypted key storage, model discovery, and token-usage display.
- SQLite persistence for conversations, messages, providers, daily usage, and
  approved edit history.
- Separate Chat and Coding workspaces, with a project root associated with each
  coding conversation.
- Project file listing and reading with path confinement, excluded sensitive
  paths, a 256 KB per-file limit, and bounded context snapshot sizes.
- Proposed edits are staged for review; applying requires approval and checks
  that the source file has not changed. Edit history supports guarded undo.
- Background commands require approval and are restricted to a command-prefix
  allowlist. Commands run without a shell and have a timeout and bounded output.
- Optional Basic authentication, configurable CORS, request metadata logging,
  and per-conversation sliding-window rate limits.
- Streaming chat responses and provider-reported completion-token usage where
  available.

## Findings

### High: provider test endpoint returns saved API keys

`POST /api/providers/test` falls back to keys stored for `provider_id` when the
request does not include a new key, then includes every key in the successful
response's `valid_keys` field (`app.py`, `test_provider`). This is not just a
static concern: an isolated test created a provider with a sentinel key, tested
it by provider ID, and confirmed the response contained that saved key.

This defeated the intended masking/encryption boundary for any client able to
call the endpoint. Basic authentication is optional in development, increasing
the exposure if the app is reachable beyond its own user. **Fixed after the
audit snapshot:** the endpoint now returns only success and models, and a
regression test checks both stored-key and submitted-key paths.

### High when exposed beyond a trusted local user: arbitrary provider URLs

`normalize_base_url` accepts any syntactically valid `http` or `https` URL.
`fetch_models` and chat requests send server-side requests to that configured
URL. The UI explicitly supports local Ollama/LM Studio endpoints, so rejecting
all private or loopback destinations would break a documented use case.
However, a network-reachable instance can be induced to make requests to
arbitrary hosts, including internal services. Keep the default loopback-only
deployment; before enabling remote access, introduce an explicit outbound-host
policy that retains deliberate local-provider support and blocks unsafe
redirects and unexpected private destinations.

### Medium: request throttling is process-local and not comprehensive

The sliding-window counter is held in an in-memory dictionary and is applied
only by selected conversation routes. It does not coordinate across workers
or restarts and does not limit provider management, conversation creation, or
requests globally by IP. It is useful for accidental bursts, but is not a
deployment-grade abuse control on its own.

### Medium: transient upstream failures are not retried with backoff

Chat streaming tries keys after authentication rejection, but does not retry
HTTP 429 or 5xx responses with exponential backoff. The only repeated request
is a compatibility retry when `stream_options` is rejected. Provider model
discovery makes one request. A rate-limit or temporary provider outage
therefore becomes an immediate failed chat rather than a bounded retry.

### Medium: project scans can do unbounded traversal work

The number of files placed in the model context is capped, but
`workspace_candidates` collects and sorts all eligible files before
`workspace_snapshot` applies that cap. Selecting a very large project can
therefore incur substantial file-system traversal, stat calls, and temporary
memory use even though only 100 files are used.

### Low: token values are estimates in part of the usage UI

User-message tokens are estimated from character count. Provider usage is used
for assistant completion tokens when available; the frontend labels totals as
approximate. These figures are helpful for rough comparison, not billing or
exact context accounting.

### Low: frontend depends on third-party CDNs

Markdown rendering and sanitization load from jsDelivr at runtime. The UI
requires external network access for those libraries, and the script tags do
not specify Subresource Integrity hashes. Consider pinned local copies or
verified integrity metadata if offline use or stronger supply-chain assurance
is required.

## Verified baseline

- SQLite `PRAGMA integrity_check`: `ok` for the live database and both existing
  backup files.
- Live database: 6 conversations, 38 messages, 11 providers, 0 edit-history
  rows.
- The live database has not yet been opened by the current app migration and
  its `conversations` table does not currently include `workspace_type`.
  Application startup adds that column with the existing migration code. The
  live database was intentionally not opened for writing during this work.
- Existing backup database files: 5 conversations, 36 messages, 11 providers.
  Their hashes differ from the live database. These are valid older snapshots,
  not copies of the current data; preserve them, and make a fresh backup before
  any schema or data migration.
- A fresh pre-migration SQLite backup was subsequently created at
  `C:\Users\LEGION\Desktop\oxion-chat-backup\chat.db-before-workspace-migration-20261030-002237.sqlite3`.
  Its integrity check is `ok` and it matches the live database's counts:
  6 conversations, 38 messages, and 11 providers.
- Audit baseline `python -m pytest -q`: 13 passed. The run emitted 82 deprecation warnings
  from the installed Starlette/FastAPI stack under Python 3.14.
- After the credential-response fix, `python -m pytest -q`: 14 passed, with
  84 dependency deprecation warnings.
- Ruff: passed.
- Black on the updated test file: passed. The pre-existing `app.py` Black check
  still reports that it would reformat the file.
- isort: passed.
- mypy: passed for the configured `app.py` checks.
- Frontend inline JavaScript syntax check: passed.
- No local listener was found on the inspected development ports at audit
  time. A process listing alone cannot prove that no other interface or port is
  in use.
- At the audit baseline there were no tests covering provider API-key
  confidentiality; the follow-up adds coverage for stored and submitted keys.
  Outbound URL policy, real upstream streaming/retry behavior, and browser
  interaction remain untested.

## Prioritized roadmap

1. **Completed: fix and test credential disclosure.** Provider-test responses
   no longer return key material; regression coverage checks stored and
   submitted-key paths.
2. **Define remote-access policy.** Keep loopback as the default. If remote
   access is a requirement, add a deliberate outbound URL policy compatible
   with explicitly configured local model servers, plus CSRF and deployment
   guidance.
3. **Harden resilience and resource bounds.** Add bounded backoff for
   retryable upstream status codes, cap project traversal work, and bound
   in-memory command-job and rate-limit state.
4. **Expand focused test coverage.** Add isolated tests for provider secret
   handling, upstream retries/streaming, URL validation, and workspace scan
   limits. Continue using temporary databases for migration tests.
5. **Improve release hygiene.** Resolve the `app.py` Black discrepancy, reduce
   dependency deprecation warnings through a tested dependency update, and
   consider local/SRI-pinned frontend libraries.
6. **Protect user data before implementation.** Since the existing backup
   snapshots differ from the live database, create and verify a fresh,
   timestamped SQLite backup before any migration or operation that can change
   persistent data. Keep existing provider and key data intact.

## Known limitations and compatibility notes

- Provider support is OpenAI-compatible HTTP APIs; there are no separate
  vendor-native adapters.
- Approved command jobs and rate limits are process-local. Pending edit
  proposals are also lost on restart.
- Context token counts are character-based estimates. Daily budget support is
  provider-specific and not a universal upstream quota API.
- The app is designed to bind to loopback by default. Optional Basic
  authentication and CORS settings do not by themselves make direct public
  exposure safe.
- The new skill registry runs trusted local `skill.py` plugins in the app
  process; it is not a Python sandbox. Review skill code before installing or
  enabling custom skills. `skill run` prepares prompt context only; it does not
  independently call a provider or execute tools.
- This pass did not implement the attached enterprise roadmap's WebSocket
  collaboration, OAuth/RBAC, Redis/PostgreSQL/SQLAlchemy conversion, cloud
  infrastructure, React migration, or load-test environment. These are
  multi-phase architecture changes; network collaboration and cloud
  deployment were also outside the confirmed local-only deployment scope.
- The audit itself was read-only. Later code changes removed API keys from
  provider-test responses, added regression tests, made Coding projects
  conversation-scoped and explicit, and added the local skill system. After the
  user confirmed local-only access, `scripts\start.sh` was aligned with the
  Windows launcher to bind loopback unconditionally. No schema, provider
  setting, key, conversation, or message in the live database was changed.
