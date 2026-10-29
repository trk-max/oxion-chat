# Changelog

## Unreleased

- Added a Vercel FastAPI entrypoint, persistent Neon/PostgreSQL schema migrations, production encryption-key configuration, and deployment documentation for online Chat.
- Added responsive phone layouts with a keyboard-accessible conversation drawer, larger touch targets, safe-area spacing, and hosted capability detection that keeps local-only Coding controls off the hosted service.
- Added project-folder selection and isolation per conversation while preserving the existing chat database and provider settings.
- Added persisted approved-edit history with guarded per-edit undo.
- Added confirmed, allowlisted background command jobs with bounded output.
- Added optional Basic authentication, configurable CORS, request metadata logging, and per-conversation rate limiting.
- Separated Chat and Coding conversation lists and fixed Coding workspace panel sizing to prevent overlap.
- Made each Coding conversation require an explicitly selected project; new chats no longer inherit the previous project's path, and Chat conversations cannot browse Coding project files.
- Added auto-discovered coding, testing, and debug skills with keyword triggers, persisted activation, a manual selector, and skill-management APIs/CLI.
- Added project-level tests, documentation, and development configuration.
