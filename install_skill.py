"""Install the self-contained Claude skill without overwriting an existing copy."""

import argparse
from pathlib import Path
import shutil


def install(target: Path) -> None:
    source = Path(__file__).parent / ".claude" / "skills" / "factiva-research"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env", "*.env"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home() / ".claude" / "skills" / "factiva-research")
    args = parser.parse_args()
    try:
        install(args.target)
    except FileExistsError:
        parser.exit(1, f"Already exists; nothing overwritten: {args.target}\n")
    print(f"Installed: {args.target}\nOpen Claude Code and use /factiva-research")
