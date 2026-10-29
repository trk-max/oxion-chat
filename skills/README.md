# Skills

Skills provide an optional, prompt-specialized layer for Chat and Coding
conversations. They are discovered from subdirectories of this folder; each
skill has a `config.yaml` and may have a `skill.py` implementation.

Use the **Skills** control in My Only Chat to enable or disable skills. When a
message matches multiple enabled triggers, the skill with the highest
priority is selected. A user can select a skill explicitly instead of using
automatic trigger matching.

Built-in skills:

- `coding`: implementation and refactoring guidance
- `testing`: test discovery, test writing, and result reporting
- `debug`: evidence-based error diagnosis

`python scripts/cli.py skill ls` lists skills. `skill create <name>` copies the
template. `skill edit <name>` opens its configuration in the configured
editor. `skill run <name> <task>` prints the prompt/context prepared for the
existing assistant workflow; it does not call a provider or modify files.

Skill Python files are trusted code and run in the application process. Review
them before enabling. The system does not pass API keys to skills. Declared
tools describe the existing approved file/command workflow; they do not grant
skills a shell or direct write access.
