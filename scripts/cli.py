"""Small command-line interface for managing My Only Chat skills."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.skills import SkillRegistry  # noqa: E402
from scripts.create_skill import create_skill  # noqa: E402

SKILLS_DIR = PROJECT_ROOT / "skills"


def build_parser() -> argparse.ArgumentParser:
    """Build the nested `skill` command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="group", required=True)
    skill = commands.add_parser("skill", help="manage assistant skills")
    actions = skill.add_subparsers(dest="action", required=True)

    actions.add_parser("ls", help="list discovered skills")
    create = actions.add_parser("create", help="create a skill from the template")
    create.add_argument("name")
    create.add_argument("--dry-run", action="store_true")
    edit = actions.add_parser("edit", help="open a skill's YAML config")
    edit.add_argument("name")
    activate = actions.add_parser("activate", help="enable a skill")
    activate.add_argument("name")
    deactivate = actions.add_parser("deactivate", help="disable a skill")
    deactivate.add_argument("name")
    run = actions.add_parser("run", help="prepare a task with one skill")
    run.add_argument("name")
    run.add_argument("task", nargs="+")
    return parser


def main() -> int:
    """Dispatch a skill-management command."""
    parser = build_parser()
    args = parser.parse_args()
    registry = SkillRegistry(SKILLS_DIR)

    try:
        if args.action == "ls":
            for config in registry.list_skills():
                state = "enabled" if config.enabled else "disabled"
                print(f"{config.name:12} {state:8} {config.description}")
            return 0
        if args.action == "create":
            path = create_skill(args.name, dry_run=args.dry_run)
            if not args.dry_run:
                print(f"Created skill at {path}")
            return 0
        if args.action == "edit":
            registry.get_skill(args.name)
            config_path = SKILLS_DIR / args.name / "config.yaml"
            editor = os.getenv("EDITOR") or ("notepad" if os.name == "nt" else "vi")
            subprocess.run(
                [*shlex.split(editor, posix=os.name != "nt"), str(config_path)],
                check=True,
            )
            return 0
        if args.action == "activate":
            registry.activate_skill(args.name)
            print(f"Enabled {args.name}")
            return 0
        if args.action == "deactivate":
            registry.deactivate_skill(args.name)
            print(f"Disabled {args.name}")
            return 0
        if args.action == "run":
            print(registry.execute_skill(args.name, " ".join(args.task), {}))
            return 0
    except (KeyError, PermissionError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
