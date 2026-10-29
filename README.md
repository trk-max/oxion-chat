# My Only Chat

My Only Chat is a FastAPI chat and coding assistant. Run it locally for project
coding, or deploy Chat online with Vercel and Neon so conversations remain
available when your PC is off.

## Run on Windows

1. Install Python 3.10 or later.
2. Double-click `run.cmd`, or run it from PowerShell:

   ```powershell
   .\run.cmd
   ```

   The launcher creates `.venv`, installs `requirements.txt` on first run, and starts the app at <http://127.0.0.1:8000>.
3. Configure an API provider in the **Providers** dialog. Providers use OpenAI-compatible chat-completions endpoints. Keys saved through the UI are encrypted with Windows DPAPI.
4. Choose **Coding** and select **New Coding Chat**. Enter that chat's absolute local project-folder path and select **Open project** before sending a coding request. Each Coding chat starts without a project and remembers only the project you explicitly open for it.

The server runs on the computer that starts it; project paths must refer to folders accessible to that server. File browsing excludes credentials, environment files, private keys, common build folders, and non-text/oversized files.

## Coding workflow

- Chat and Coding keep separate conversation lists. New conversations are saved in the selected list; switching workspaces clears the active conversation rather than carrying it into the other list. Existing per-chat mode preferences from older builds are migrated into the matching list.
- A new Coding chat does not inherit the previous chat's project. Open a project for each Coding chat before sending; file browsing, proposed edits, commands, and undo are scoped to that chat's selected project. Chat conversations cannot open or browse Coding projects. Moving a Coding chat to Chat clears its project association.
- Coding mode lists readable project files and supplies a bounded snapshot to the model.
- The assistant can propose up to 10 file changes. The UI previews each change and checks that the original file has not changed before applying.
- Applied changes are recorded in the conversation's edit history. **Undo** restores the previous content only when the file still matches the assistant's applied version.
- Commands require an explicit confirmation and must match a safe prefix: `python -m pytest`, `python -m compileall`, `pytest`, `git status`, `git diff`, `git log`, or `ruff check`. Commands run without a shell in the selected project, have a 1000-character limit and a five-minute timeout, and their displayed output is capped. Test commands execute project code, so review the command and project before approving.

## Skills

Skills add specialized prompts to matching messages. The built-in `coding`,
`testing`, and `debug` skills are discovered from `skills/` and can be enabled
or disabled in the **Skills** panel. Choose **Automatic skill** to use triggers,
or select one skill manually. Configuration is stored in each skill's
`config.yaml`; YAML parsing uses PyYAML.

```powershell
python scripts\cli.py skill ls
python scripts\cli.py skill create review_helper --dry-run
python scripts\cli.py skill create review_helper
python scripts\cli.py skill activate coding
python scripts\cli.py skill deactivate coding
python scripts\cli.py skill run testing "write regression tests"
```

`python scripts\cli.py skill edit <name>` opens the skill's YAML file in
`EDITOR` (or Notepad on Windows). `python scripts\create_skill.py <name>`
generates a skill from the template. Skills are trusted local Python code
loaded into this application's process; review custom skill code before use.
Skills receive no API keys and do not bypass the existing edit/command approval
flows. `skill run` prepares prompt and task context only; it does not call a
provider or execute project tools.

## Providers and keys

Provider settings, keys, model choices, and provider usage are managed in the
existing Providers UI. Local mode stores them in SQLite; online mode stores
them in Neon PostgreSQL. On Windows, local saved keys are encrypted with DPAPI.
On the hosted service, keys are encrypted with the configured Fernet
`CHAT_ENCRYPTION_KEY`.

Multiple keys can be registered for a provider. The app can try another key after an authentication rejection and tracks configured token budgets. Do not copy credentials into source files or commit `.env` files.

## Security and deployment

- The default launcher binds to `127.0.0.1`; keep it local unless you configure a hardened HTTPS reverse proxy.
- The Windows and Unix launchers bind only to `127.0.0.1`; the Unix launcher does not accept a `HOST` override.
- Optional Basic authentication is enabled by setting both `ADMIN_USER` and `ADMIN_PASSWORD`. Production mode (`APP_ENV=production`) requires a password of at least 16 characters.
- `CORS_ORIGINS` is a comma-separated origin allowlist. Wildcard origins are rejected in production. Leave it empty for the same-origin local UI.
- Requests are logged with method, path, status, duration, client IP, and user-agent; request bodies, authorization headers, and API keys are not logged.
- Conversation-scoped endpoints enforce a sliding-window limit of 60 requests per minute.
- Paths are normalized and confined to the selected project. File content is read as UTF-8 and limited to 256 KB for workspace browsing. Never expose the local server directly to the public internet.

Copy `.env.example` to `.env` only if you need environment configuration. Existing provider credentials in `chat.db` are not moved or overwritten by this integration.

## Use Chat online while your PC is off

The app can run as a Vercel Python function with Neon PostgreSQL for persistent
online conversations. Once deployed, Vercel serves the app independently of
your computer, so you can open the same deployment from another device while
your PC is shut down.

1. Push this project to a **private** GitHub repository. Do not commit `.env`,
   database files, provider keys, or encryption keys.
2. Create a Neon PostgreSQL database and copy its pooled connection string
   (`DATABASE_URL`) from the Neon Console.
3. Import the private repository into Vercel and add these Production
   environment variables in **Project Settings → Environment Variables**:
   - `APP_ENV=production`
   - `DATABASE_URL` — the Neon connection string; keep it secret.
   - `ADMIN_USER` — the username for the browser's Basic-auth prompt.
   - `ADMIN_PASSWORD` — a unique password with at least 16 characters.
   - `CHAT_ENCRYPTION_KEY` — a Fernet key generated with:
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `CORS_ORIGINS` can remain unset for the same-origin Vercel app.
4. Deploy. The app creates or upgrades its PostgreSQL tables on startup. Add
   your provider and API key in the deployed app's Providers dialog; provider
   keys are encrypted using `CHAT_ENCRYPTION_KEY`.

The deployed service uses a **new online database**; local conversations,
provider settings, and DPAPI-encrypted Windows keys are not uploaded or
migrated. Re-enter provider keys on the online deployment. Keep the same
`CHAT_ENCRYPTION_KEY` across deployments or saved online provider keys cannot
be decrypted.

This deployment supports online **Chat**. Coding workspaces still access
folders on the machine running the app; Vercel cannot access your PC's files,
so project browsing and editing are deliberately unavailable in the hosted
deployment. Run the app locally for Coding. Hosted command execution is not
available. Vercel's configured function limit is 60 seconds; long provider
responses may be terminated by the hosting platform.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Serve the chat UI |
| `GET` | `/api/capabilities` | Report whether hosted Coding workspaces are available |
| `GET` | `/api/conversations` | List saved conversations |
| `GET` | `/api/conversations?workspace_type=chat\|coding` | List only Chat or Coding conversations |
| `POST` | `/api/conversations` | Create a conversation (`workspace_type` and optional `project_root`) |
| `GET` | `/api/conversations/{id}` | Read conversation and message history |
| `POST` | `/api/conversations/{id}/workspace` | Move a conversation to Chat or Coding |
| `DELETE` | `/api/conversations/{id}` | Delete one conversation and its edit history |
| `POST` | `/api/chat` | Send a message and receive a streamed SSE response |
| `GET` | `/api/workspace/tree?conv_id={id}` | List safe project files for one conversation |
| `GET` | `/api/workspace/file?path=...&conv_id={id}` | Read one safe project file |
| `POST` | `/api/conversations/{id}/project` | Associate a local project folder with a Coding conversation |
| `POST` | `/api/workspace/preview` | Stage a proposed edit for review |
| `POST` | `/api/workspace/{review_id}/apply` | Apply an edit after `{"approved": true}` |
| `GET` | `/api/conversations/{id}/edits` | List applied edits |
| `POST` | `/api/conversations/{id}/edits/{edit_id}/undo` | Undo an edit after explicit approval |
| `POST` | `/api/conversations/{id}/commands` | Queue an allowlisted command after approval |
| `GET` | `/api/commands/{job_id}?conv_id={id}` | Read command status and output |
| `GET`, `POST`, `PATCH`, `DELETE` | `/api/providers...` | Manage, test, activate, and inspect providers |
| `GET` | `/api/skills` | List discovered skills and activation state |
| `POST` | `/api/skills/{name}/activate` | Enable a skill |
| `POST` | `/api/skills/{name}/deactivate` | Disable a skill |
| `POST` | `/api/skills/execute` | Prepare a task with one enabled skill's prompt |
| `GET`, `POST` | `/api/v1/skills` | Compatibility aliases for skill listing |
| `POST` | `/api/v1/skills/{name}/activate` | Versioned alias to enable a skill |
| `POST` | `/api/v1/skills/{name}/deactivate` | Versioned alias to disable a skill |
| `POST` | `/api/v1/skills/execute` | Versioned alias to prepare a skill task |

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## Tests and development

```powershell
python -m pip install -r requirements.txt
python -m pytest
python -m ruff check app.py tests
```

`make test` and `make lint` are also available where GNU Make is installed. `scripts/start.sh` provides a one-command launcher on Unix-like systems. Formatting and type-check settings live in `pyproject.toml`.

## Data and limitations

- Conversations, messages, providers, and applied edit history are stored locally in `chat.db` (or `CHAT_DB`).
- Token usage is shown for every chat in the chat list and for the active chat. Input counts are estimates; output counts use provider-reported completion tokens when available. Credit cost and context-window percentages are intentionally not shown.
- Staged proposals and active command jobs are held in memory and disappear when the server restarts.
- Command jobs and rate-limit counters are process-local; they are not coordinated across multiple workers.
- Provider quota data depends on the selected API provider and is not a guarantee of upstream availability.
