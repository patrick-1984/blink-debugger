"""Persist generalized Blink Debugger software improvement notes locally."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat

try:
    import sqlite3
except ImportError:
    sqlite3 = None

STORAGE_ERRORS = (OSError, sqlite3.Error) if sqlite3 is not None else (OSError,)


KINDS = ("fix", "feature")
TEXT_LIMIT = 320
SUGGESTION_LIMIT = 8
PUNCTUATION = " .,:;!?()'_-+"
PRIVATE_PATTERNS = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b|"
    r"\b[0-9a-f]{1,4}::[0-9a-f:]*\b|"
    r"\b(?:authorization|password|secret|token|cookie|private key|serial(?: number)?)\s*:",
    re.IGNORECASE,
)


def lesson_text(value):
    if (not isinstance(value, str) or len(value) > TEXT_LIMIT
            or any(not (char.isalnum() or char in PUNCTUATION) for char in value)):
        raise ValueError("Use a short, single-line software observation or suggestion in plain prose.")
    normalized = " ".join(value.split())
    if not normalized or PRIVATE_PATTERNS.search(normalized):
        raise ValueError("Use general software wording without credentials, addresses, or identifiers.")
    return normalized


def state_path():
    override = os.environ.get("BLINK_DEBUGGER_STATE_DIR")
    if override:
        return Path(override).expanduser().absolute()
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    return Path(base).expanduser().absolute() / "blink-debugger"


def require_private(path, expected):
    info = path.lstat()
    if not expected(info.st_mode):
        raise ValueError("The software lesson store requires a private directory and regular database.")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("The software lesson store must be private to the current user.")


def database_path(write=False):
    root = state_path()
    if write:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.exists() and not root.is_symlink():
        return None
    require_private(root, stat.S_ISDIR)
    database = root / "software-lessons.sqlite3"
    if write:
        try:
            fd = os.open(str(database), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    if not database.exists() and not database.is_symlink():
        return None
    require_private(database, stat.S_ISREG)
    return database


def check_store_access(write=False):
    """Check local state access without initializing a directory or database."""
    if sqlite3 is None:
        raise ValueError("The Python SQLite component is required for local lessons.")
    root = state_path()
    try:
        path = database_path()
        if path is not None and not os.access(path, os.R_OK):
            raise ValueError("The software lesson database requires read access.")
        if not write:
            return path
        ancestor = root
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            raise ValueError("The software lesson store requires a writable directory.")
        if path is not None and not os.access(path, os.W_OK):
            raise ValueError("The software lesson database requires write access.")
        return path
    except OSError:
        raise ValueError("The local software lesson store is unavailable.") from None


def record_lesson(kind, observation, suggestion):
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("Choose a software fix or feature.")
    entry = {"kind": kind, "observation": lesson_text(observation), "suggestion": lesson_text(suggestion)}
    identifier = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    try:
        check_store_access(write=True)
        path = database_path(write=True)
        with closing(sqlite3.connect(str(path), timeout=4)) as database, database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS lessons (id TEXT PRIMARY KEY, kind TEXT, observation TEXT, suggestion TEXT)"
            )
            cursor = database.execute(
                "INSERT OR IGNORE INTO lessons (id, kind, observation, suggestion) VALUES (?, ?, ?, ?)",
                (identifier, entry["kind"], entry["observation"], entry["suggestion"]),
            )
            return {"schemaVersion": 1, "id": identifier, "recorded": cursor.rowcount == 1}
    except STORAGE_ERRORS:
        raise ValueError("The local software lesson store is unavailable.") from None


def suggest_lessons():
    try:
        path = check_store_access()
        if path is None:
            return {"schemaVersion": 1, "suggestions": []}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=4)) as database:
            table = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lessons'").fetchone()
            rows = [] if table is None else database.execute(
                "SELECT id, kind, observation, suggestion FROM lessons ORDER BY rowid DESC LIMIT ?",
                (SUGGESTION_LIMIT,),
            ).fetchall()
        fields = ("id", "kind", "observation", "suggestion")
        return {"schemaVersion": 1, "suggestions": [dict(zip(fields, row)) for row in rows]}
    except STORAGE_ERRORS:
        raise ValueError("The local software lesson store is unavailable.") from None
