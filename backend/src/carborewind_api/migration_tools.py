"""Mailbox lists for migrations: Excel/CSV import, template, automatic matching.

Import
------
``.xlsx`` (first sheet) or ``.csv`` (``,`` ``;`` or tab, UTF-8 or Windows-1252). The first row
names the columns, in English or French, case and accents ignored::

    source_email | source_login | password | destination | dossier

Only the source address is required. Without a header row, the columns are read as
``source, destination, password, login, folder`` (a plain list of addresses works). The file
is parsed in memory and never stored; its size, its uncompressed size (an ``.xlsx`` is a zip)
and its number of rows are bounded.

Matching
--------
A source address is matched to a Carbonio account, in this order:

1. the destination given in the file or by hand (address or alias) — ``manual``;
2. an account with the very same address — ``exact``;
3. an account having it as an alias — ``alias``;
4. an account with the same local part (``ana@old.com`` -> ``ana@new.com``), restricted to the
   chosen destination domain when there is one — ``local_part``. Several candidates and no
   domain to decide: no match, the candidates are listed.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
MAX_ROWS = 5000
EMAIL_RE = re.compile(r"^[^@\s\"<>,;]+@[A-Za-z0-9.-]+\.[A-Za-z0-9-]{2,}$")
ROOT_RE = re.compile(r"^[^/\x00-\x1f]{1,128}$")

COLUMNS = ("source_email", "source_login", "password", "target_email", "root")
# Order of the columns of a file without header row.
POSITIONAL = ("source_email", "target_email", "password", "source_login", "root")
HEADERS = {
    "source_email": (
        "sourceemail",
        "source",
        "emailsource",
        "sourceaddress",
        "adressesource",
        "ancienneadresse",
        "email",
        "mail",
        "adresse",
        "from",
    ),
    "source_login": ("sourcelogin", "login", "identifiant", "username", "utilisateur", "user"),
    "password": ("password", "sourcepassword", "motdepasse", "mdp", "pass", "pwd"),
    "target_email": (
        "destination",
        "destinationemail",
        "target",
        "targetemail",
        "adressedestination",
        "nouvelleadresse",
        "cible",
        "carbonio",
        "to",
    ),
    "root": ("dossier", "dossiercible", "targetfolder", "folder", "root", "rootfolder"),
}
_HEADER_INDEX = {alias: column for column, aliases in HEADERS.items() for alias in aliases}


class ImportFileError(ValueError):
    pass


@dataclass
class Row:
    line: int
    source_email: str
    source_login: str | None = None
    password: str | None = None
    target_email: str | None = None
    root: str | None = None

    def __repr__(self) -> str:  # never print the password
        return f"Row({self.line}, {self.source_email} -> {self.target_email})"


@dataclass
class RowError:
    line: int
    message: str


@dataclass
class ParsedFile:
    rows: list[Row] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", text)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _xlsx_rows(data: bytes) -> list[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_UNZIPPED_BYTES:
                raise ImportFileError("file too large once uncompressed")
    except zipfile.BadZipFile as exc:
        raise ImportFileError("not a valid .xlsx file") from exc
    from openpyxl import load_workbook  # heavy import, only when needed

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types on malformed files
        raise ImportFileError("not a valid .xlsx file") from exc
    try:
        sheet = workbook.worksheets[0]
        rows: list[list[str]] = []
        for values in sheet.iter_rows(values_only=True):
            rows.append([_cell(v) for v in values])
            if len(rows) > MAX_ROWS + 1:
                raise ImportFileError(f"more than {MAX_ROWS} rows")
        return rows
    finally:
        workbook.close()


def _csv_rows(data: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost anything
        raise ImportFileError("unknown text encoding")
    if "\x00" in text:
        raise ImportFileError("not a text or .xlsx file")
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = []
    for values in csv.reader(io.StringIO(text), delimiter=delimiter):
        rows.append([v.strip() for v in values])
        if len(rows) > MAX_ROWS + 1:
            raise ImportFileError(f"more than {MAX_ROWS} rows")
    return rows


def parse_file(data: bytes) -> ParsedFile:
    """Rows of an uploaded ``.xlsx`` / ``.csv`` file, with one error per invalid line."""
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError("file too large")
    raw_rows = _xlsx_rows(data) if data[:4] == b"PK\x03\x04" else _csv_rows(data)
    if not raw_rows:
        raise ImportFileError("empty file")
    header = [_HEADER_INDEX.get(_norm(v)) for v in raw_rows[0]]
    if "source_email" in header:
        mapping = {i: col for i, col in enumerate(header) if col}
        body = list(enumerate(raw_rows[1:], start=2))
    elif raw_rows[0] and EMAIL_RE.match(raw_rows[0][0] or ""):
        mapping = dict(enumerate(POSITIONAL))
        body = list(enumerate(raw_rows, start=1))
    else:
        raise ImportFileError("no source address column (expected a header such as source_email)")
    result = ParsedFile()
    for line, values in body:
        if not any(values):
            continue
        fields = {col: values[i] for i, col in mapping.items() if i < len(values) and values[i]}
        row, error = make_row(line, fields)
        if error:
            result.errors.append(RowError(line, error))
        elif row:
            result.rows.append(row)
    return result


def make_row(line: int, fields: dict[str, Any]) -> tuple[Row | None, str | None]:
    """Validate one entry (from a file or typed by hand)."""
    source = str(fields.get("source_email") or "").strip().lower()
    if not EMAIL_RE.match(source) or len(source) > 320:
        return None, f"invalid source address: {source[:100]!r}"
    target = str(fields.get("target_email") or "").strip().lower() or None
    if target and (not EMAIL_RE.match(target) or len(target) > 320):
        return None, f"invalid destination address: {target[:100]!r}"
    login = str(fields.get("source_login") or "").strip() or None
    if login and (len(login) > 320 or any(c in login for c in "\r\n\x00")):
        return None, "invalid login"
    password = fields.get("password")
    password = str(password) if password not in (None, "") else None
    if password and (len(password) > 1024 or any(c in password for c in "\r\n\x00")):
        return None, "invalid password"
    root = str(fields.get("root") or "").strip().strip("/") or None
    if root and not ROOT_RE.match(root):
        return None, "invalid folder name"
    return Row(line, source, login, password, target, root), None


# ------------------------------------------------------------------ template


TEMPLATE_HEADER = ["source_email", "source_login", "password", "destination", "dossier"]
TEMPLATE_EXAMPLE = ["ana@ancien-domaine.com", "", "", "", ""]


def template_csv() -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(TEMPLATE_HEADER)
    writer.writerow(TEMPLATE_EXAMPLE)
    return ("﻿" + buffer.getvalue()).encode()


def template_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None  # noqa: S101 - a new workbook has one sheet
    sheet.title = "Boites"
    sheet.append(TEMPLATE_HEADER)
    sheet.append(TEMPLATE_EXAMPLE)
    for column in "ABCDE":
        sheet.column_dimensions[column].width = 32
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------ matching


@dataclass(frozen=True)
class Target:
    id: Any
    email: str
    aliases: tuple[str, ...] = ()


@dataclass
class Match:
    target: Target | None
    how: str | None  # manual, exact, alias, local_part
    candidates: list[str] = field(default_factory=list)
    error: str | None = None


class Matcher:
    def __init__(self, targets: Iterable[Target]) -> None:
        self.by_email: dict[str, Target] = {}
        self.by_alias: dict[str, Target] = {}
        self.by_local: dict[str, list[Target]] = {}
        for t in targets:
            self.by_email[t.email.lower()] = t
            self.by_local.setdefault(t.email.lower().split("@")[0], []).append(t)
            for alias in t.aliases:
                self.by_alias.setdefault(alias.lower(), t)

    def resolve(self, address: str) -> Target | None:
        key = address.lower()
        return self.by_email.get(key) or self.by_alias.get(key)

    def match(self, source: str, explicit: str | None = None, domain: str | None = None) -> Match:
        if explicit:
            target = self.resolve(explicit)
            if target is None:
                return Match(None, None, error=f"destination {explicit} not found in Carbonio")
            return Match(target, "manual")
        key = source.lower()
        if key in self.by_email:
            return Match(self.by_email[key], "exact")
        if key in self.by_alias:
            return Match(self.by_alias[key], "alias")
        candidates = self.by_local.get(key.split("@")[0], [])
        if domain:
            candidates = [c for c in candidates if c.email.lower().endswith("@" + domain.lower())]
        if len(candidates) == 1:
            return Match(candidates[0], "local_part")
        return Match(None, None, candidates=sorted(c.email for c in candidates))
