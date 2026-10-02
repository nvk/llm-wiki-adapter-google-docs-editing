#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from google_docs_adapter.api_operations import API_RESOURCE  # noqa: E402
from google_docs_adapter.oauth import ACCESS_TOKEN_ENV, OAUTH_DIR_ENV  # noqa: E402

STATE_DIR_ENV = "LLM_WIKI_GOOGLE_DOCS_STATE_DIR"


def _version_key(path: Path) -> tuple[int, ...]:
    try:
        return tuple(int(value) for value in path.parents[1].name.split("."))
    except ValueError:
        return (0,)


def find_llm_wiki(explicit: str | None = None) -> Path:
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        command = shutil.which("llm-wiki")
        candidates = [Path(command)] if command else []
        candidates.extend(
            sorted(
                Path.home().glob(".codex/plugins/cache/llm-wiki/wiki/*/bin/llm-wiki"),
                key=_version_key,
                reverse=True,
            )
        )
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise RuntimeError("could not find an executable llm-wiki; pass --llm-wiki")


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def registration_command(
    llm_wiki: Path,
    adapter_root: Path,
    data_dir: Path,
) -> list[str]:
    input_dir = data_dir / "input"
    output_dir = data_dir / "output"
    command = [
        str(llm_wiki),
        "adapter",
        "add",
        str(adapter_root),
        "--replace",
        "--read-root",
        str(input_dir),
        "--read-root",
        str(output_dir),
        "--write-root",
        str(output_dir),
        "--remote-resource",
        API_RESOURCE,
        "--env",
        ACCESS_TOKEN_ENV,
        "--env",
        OAUTH_DIR_ENV,
        "--env",
        STATE_DIR_ENV,
        "--json",
    ]
    return command


def _run(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("llm-wiki adapter registration failed")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("llm-wiki returned invalid registration output") from exc
    if not isinstance(value, dict):
        raise RuntimeError("llm-wiki returned invalid registration output")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install and register the Google Docs API adapter locally"
    )
    parser.add_argument("--llm-wiki")
    parser.add_argument(
        "--data-dir",
        default=str(
            Path.home() / ".local" / "share" / "llm-wiki" / "google-docs-editing"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        llm_wiki = find_llm_wiki(args.llm_wiki)
        adapter_root = REPOSITORY_ROOT.resolve(strict=True)
        data_dir = Path(args.data_dir).expanduser().resolve(strict=False)
        command = registration_command(
            llm_wiki,
            adapter_root,
            data_dir,
        )
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "dry_run": True,
                        "adapter_root": str(adapter_root),
                        "data_dir": str(data_dir),
                        "browser_transport": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if not (adapter_root / ".venv" / "bin" / "python").is_file():
            venv.EnvBuilder(with_pip=False).create(adapter_root / ".venv")
        private_directory(data_dir)
        private_directory(data_dir / "input")
        private_directory(data_dir / "output")
        _run(command)
        doctor = subprocess.run(
            [
                str(llm_wiki),
                "adapter",
                "doctor",
                "google-docs-editing",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if doctor.returncode != 0:
            raise RuntimeError("adapter registered but doctor failed")
        print(
            json.dumps(
                {
                    "status": "ok",
                    "adapter_id": "google-docs-editing",
                    "data_dir": str(data_dir),
                    "browser_transport": False,
                    "doctor": "ok",
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
