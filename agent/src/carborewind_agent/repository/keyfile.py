"""Reading and creating the KEK file with strict permissions."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .crypto import decode_key, encode_key, generate_key


class KeyFileError(Exception):
    pass


def read_secret(path: Path) -> str:
    """Read a secret file (first line), refusing one accessible by group or others."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as exc:
        raise KeyFileError(f"secret file {path} not found") from exc
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFileError(f"secret file {path} must not be accessible by group/others (chmod 600)")
    lines = path.read_text().splitlines()
    value = lines[0].strip() if lines else ""
    if not value:
        raise KeyFileError(f"secret file {path} is empty")
    return value


def read_kek(path: Path) -> bytes:
    """Load the KEK, refusing a file readable by group or others."""
    text = read_secret(path)
    try:
        return decode_key(text)
    except ValueError as exc:
        raise KeyFileError(f"key file {path} is invalid: {exc}") from exc


def create_kek(path: Path) -> None:
    """Write a new random KEK (mode 0600). Never overwrites an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(encode_key(generate_key()) + "\n")
