"""Command-line entry point: ``carborewind-agent``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from . import __version__
from .config import AgentSettings


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carborewind-agent", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="validate configuration and TLS files")
    args = parser.parse_args(argv)
    if args.command == "check-config":
        return check_config()
    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
