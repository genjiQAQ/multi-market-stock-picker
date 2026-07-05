#!/usr/bin/env python3
"""Run stock_picker with a skill-local virtual environment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
PICKER_MODULE = "stock_picker.cli"
CONFIG_PATH = SKILL_DIR / "config" / "defaults.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def resolve_skill_path(value: str | Path, *, skill_dir: Path = SKILL_DIR) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return skill_dir / path


def default_venv_dir() -> Path:
    config = load_config()
    return resolve_skill_path(config.get("venv_dir", ".venv"))


def default_requirements_file() -> Path:
    config = load_config()
    return resolve_skill_path(config.get("requirements_file", "requirements.txt"))


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_or_update_venv(venv_dir: Path) -> None:
    requirements_path = default_requirements_file()
    if not requirements_path.exists():
        raise FileNotFoundError(f"requirements file not found: {requirements_path}")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    if not venv_python(venv_dir).exists():
        builder = venv.EnvBuilder(with_pip=True)
        builder.create(venv_dir)
    subprocess.run(
        [str(venv_python(venv_dir)), "-m", "pip", "install", "-r", str(requirements_path)],
        check=True,
    )


def choose_python(venv_dir: Path) -> Path:
    python = venv_python(venv_dir.expanduser())
    if python.exists():
        return python
    return Path(sys.executable)


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap and run the multi-market stock picker. Use --setup once "
            "to create the skill-local Python environment."
        ),
        epilog=(
            "Picker arguments passed through include: --market, --universe, "
            "--custom-mode, --symbols, --symbols-file, --style, --top-n, "
            "--max-candidates, --run-mode, --out-dir, --config, --no-cache, --live, "
            "--ai-narrative, --ai-model, --ai-base-url, and --ai-narrative-limit."
        ),
    )
    parser.add_argument("--setup", action="store_true", help="Create/update the skill-local venv and install requirements.txt")
    parser.add_argument("--venv-dir", default=str(default_venv_dir()), help="Skill-local venv directory")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Config passed to stock_picker")
    parser.add_argument("--print-command", action="store_true", help="Print the resolved stock_picker command without running it")
    args, picker_args = parser.parse_known_args(argv)
    return args, picker_args


def main(argv: list[str] | None = None) -> int:
    args, picker_args = parse_args(argv)
    venv_dir = Path(args.venv_dir).expanduser()
    if args.setup:
        create_or_update_venv(venv_dir)

    python = choose_python(venv_dir)
    env = os.environ.copy()
    scripts_dir = str(SKILL_DIR / "scripts")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = scripts_dir if not existing_pythonpath else scripts_dir + os.pathsep + existing_pythonpath

    command = [
        str(python),
        "-m",
        PICKER_MODULE,
        "--config",
        str(Path(args.config).expanduser()),
    ] + picker_args
    if args.print_command:
        print(" ".join(command))
        return 0

    if python == Path(sys.executable) and not args.setup:
        print(
            "Using the current Python because the skill-local venv does not exist. "
            "For project-independent packages, run this command once with --setup.",
            file=sys.stderr,
        )
    return subprocess.run(command, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
