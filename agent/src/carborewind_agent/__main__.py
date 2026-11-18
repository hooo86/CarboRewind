"""Command-line entry point: ``carborewind-agent``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .config import AgentSettings
from .repository import IntegrityError, Repository, RepositoryError
from .repository.keyfile import KeyFileError, create_kek, read_kek


def check_config() -> int:
    """Validate the configuration and the presence of TLS material. Returns an exit code."""
    try:
        settings = AgentSettings()  # values come from the environment
    except ValidationError as exc:
        print(f"invalid configuration: {exc.error_count()} error(s)", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            print(f"  - {location}: {error['msg']}", file=sys.stderr)
        return 2
    missing = settings.missing_files()
    for path in missing:
        print(f"missing file: {path}", file=sys.stderr)
    if missing:
        return 1
    print("configuration OK")
    return 0


def _repo_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """Repository and key file: from the command line, else from the environment."""
    if args.path and args.key_file:
        return Path(args.path), Path(args.key_file)
    defaults = AgentSettings.model_fields
    path = args.path or os.environ.get(
        "CARBOREWIND_AGENT_REPOSITORY_PATH", str(defaults["repository_path"].default)
    )
    key = args.key_file or os.environ.get(
        "CARBOREWIND_AGENT_REPOSITORY_KEY_FILE", str(defaults["repository_key_file"].default)
    )
    return Path(path), Path(key)


def repo_command(args: argparse.Namespace) -> int:
    path, key_file = _repo_paths(args)
    try:
        if args.repo_command == "keygen":
            create_kek(key_file)
            print(f"new key written to {key_file} — store a copy in your escrow (see DR plan)")
            return 0
        if args.repo_command == "init":
            repo = Repository.init(path, read_kek(key_file))
            print(f"repository {repo.repo_id} created at {path}")
            return 0
        if args.repo_command == "rotate-key":
            Repository.rotate_kek(path, read_kek(key_file), read_kek(Path(args.new_key_file)))
            print("master key re-wrapped with the new key; replace the key file and escrow copy")
            return 0
        repo = Repository.open(path, read_kek(key_file))
        report = repo.verify(sample=args.sample)
    except FileExistsError:
        print(f"error: {key_file} already exists", file=sys.stderr)
        return 1
    except (KeyFileError, RepositoryError, IntegrityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for blob_id in report.corrupted:
        print(f"corrupted: {blob_id}", file=sys.stderr)
    print(f"checked {report.checked} blob(s), {len(report.corrupted)} corrupted")
    return 0 if report.ok else 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carborewind-agent", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="validate configuration and TLS files")
    repo = sub.add_parser("repo", help="manage the backup repository")
    repo.add_argument("--path", help="repository directory")
    repo.add_argument("--key-file", help="KEK file (base64, mode 0600)")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    repo_sub.add_parser("keygen", help="create a new random key file")
    repo_sub.add_parser("init", help="initialise an empty repository")
    verify = repo_sub.add_parser("verify", help="authenticate blobs (exit 3 if corrupted)")
    verify.add_argument("--sample", type=float, default=1.0, help="fraction to check (0-1]")
    rotate = repo_sub.add_parser("rotate-key", help="re-wrap the master key with a new KEK")
    rotate.add_argument("new_key_file")
    args = parser.parse_args(argv)
    if args.command == "check-config":
        return check_config()
    if args.command == "repo":
        return repo_command(args)
    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
