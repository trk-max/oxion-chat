"""Create a new skill by copying the reviewed local template."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import yaml  # type: ignore[import-untyped]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


def create_skill(name: str, *, dry_run: bool = False) -> Path:
    """Create one skill directory and substitute its config name."""
    if not SKILL_NAME_RE.fullmatch(name):
        raise ValueError("Skill names must be lowercase letters, digits, '_' or '-'")
    destination = SKILLS_DIR / name
    if destination.exists():
        raise FileExistsError(f"Skill already exists: {name}")
    template = SKILLS_DIR / "_template"
    if not template.is_dir():
        raise FileNotFoundError(f"Skill template not found: {template}")
    if dry_run:
        print(f"Would create {destination}")
        for source in sorted(template.rglob("*")):
            if source.is_file():
                print(f"  {source.relative_to(template)}")
        return destination

    shutil.copytree(template, destination)
    config_path = destination / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["name"] = name
    config["triggers"] = [f"@{name}"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    readme = destination / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("Custom skill", name),
        encoding="utf-8",
    )
    return destination


def main() -> int:
    """Run the standalone skill-template generator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        created = create_skill(args.name, dry_run=args.dry_run)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    if not args.dry_run:
        print(f"Created skill at {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
