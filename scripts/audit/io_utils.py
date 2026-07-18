"""
LightSignal — audit/io_utils.py
===============================
Loaders, the read-only guard, and the label snapshot.

Two things here exist because of problems hit in practice:

  * news_feed.csv holds 2000-char Article_Text blobs; the default csv field limit
    trips on them. Raise it before any read.
  * news_feed_test_feedback.xlsx is routinely open in Excel, which takes an
    exclusive lock — a plain open(..., 'rb') raises PermissionError, not just
    pandas. read_locked_bytes() goes through a shared-mode Win32 handle.
"""

import csv
import ctypes
import hashlib
import io
import os
import sys
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

import pandas as pd

from audit.audit_config import (
    READ_ONLY_FILES, FILE_NEWS_FEED, FILE_STAGED, FILE_DC,
    FILE_LABELS_XLSX, FILE_LABELS_SNAPSHOT,
)

csv.field_size_limit(10 ** 7)


# ── Read-only guard ───────────────────────────────────────────────────────────

def _sha256(path: Path) -> str | None:
    if not Path(path).exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def read_only_guard(files=READ_ONLY_FILES):
    """
    Hash the live data files on entry, re-hash on exit, hard-fail on any change.

    The audit's entire value rests on it being non-destructive. This makes that
    a checked property rather than an intention.
    """
    before = {Path(f): _sha256(f) for f in files}
    try:
        yield
    finally:
        changed = [
            str(f) for f, h in before.items()
            if _sha256(f) != h
        ]
        if changed:
            raise RuntimeError(
                "AUDIT WROTE TO LIVE DATA — this must never happen.\n"
                "Modified: " + "\n           ".join(changed)
            )


# ── Locked-file reads (Excel holds an exclusive lock) ─────────────────────────

def read_locked_bytes(path: Path) -> bytes:
    """
    Read a file Windows has locked for writing (e.g. an open Excel workbook).

    Plain open() raises PermissionError. CreateFileW with FILE_SHARE_READ|WRITE|DELETE
    succeeds against Excel's lock.
    """
    path = Path(path)
    try:
        return path.read_bytes()
    except PermissionError:
        pass

    if sys.platform != "win32":
        raise

    GENERIC_READ  = 0x80000000
    SHARE_ALL     = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype  = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    handle = kernel32.CreateFileW(
        str(path.resolve()), GENERIC_READ, SHARE_ALL, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle == INVALID_HANDLE or handle is None:
        raise PermissionError(
            f"Could not open {path.name} even in shared mode "
            f"(win32 error {ctypes.get_last_error()})."
        )
    try:
        buf   = ctypes.create_string_buffer(1 << 20)
        read  = wintypes.DWORD(0)
        chunks = []
        while True:
            ok = kernel32.ReadFile(
                wintypes.HANDLE(handle), buf, len(buf), ctypes.byref(read), None
            )
            if not ok or read.value == 0:
                break
            chunks.append(buf.raw[: read.value])
        return b"".join(chunks)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_news_feed() -> pd.DataFrame:
    """The live handoff file. All columns as str — scores/booleans are dirty."""
    df = pd.read_csv(FILE_NEWS_FEED, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    df = df.replace("", pd.NA)
    return df


def load_staged() -> pd.DataFrame:
    """
    Staging table. Holds up to 8000 chars of article_text — vs news_feed's 2000 —
    so this is the better text source for anything that has to actually read the
    article. Rows age out after 30 days, so it does not cover the whole backlog.
    """
    return pd.read_csv(FILE_STAGED, dtype=str, encoding="utf-8-sig", keep_default_na=False).replace("", pd.NA)


def load_dc_database() -> pd.DataFrame:
    return pd.read_csv(FILE_DC, dtype=str, encoding="utf-8-sig", keep_default_na=False).replace("", pd.NA)


def unprocessed(df: pd.DataFrame) -> pd.DataFrame:
    """Rows the user has not blessed: Processed is anything other than TRUE."""
    flag = df["Processed"].fillna("").astype(str).str.strip().str.upper()
    return df[flag != "TRUE"].copy()


# ── Label snapshot ────────────────────────────────────────────────────────────

def snapshot_labels(force: bool = False) -> pd.DataFrame:
    """
    Freeze news_feed_test_feedback.xlsx so the eval harness does not depend on the
    workbook being closed in Excel.

    Pickle rather than parquet: the repo has no pyarrow/fastparquet, and adding a
    dependency to cache one file is not worth it.
    """
    if FILE_LABELS_SNAPSHOT.exists() and not force:
        return pd.read_pickle(FILE_LABELS_SNAPSHOT)

    if not FILE_LABELS_XLSX.exists():
        raise FileNotFoundError(f"Label file not found: {FILE_LABELS_XLSX}")

    raw = read_locked_bytes(FILE_LABELS_XLSX)
    df  = pd.read_excel(io.BytesIO(raw))
    df.to_pickle(FILE_LABELS_SNAPSHOT)
    return df


# ── Boolean / numeric coercion (the CSV is dirty; be explicit about it) ───────

def as_bool(series: pd.Series) -> pd.Series:
    """
    Coerce the pipeline's boolean columns to real booleans.

    These fields arrive in four different shapes depending on the source:
      * news_feed.csv  -> 'TRUE'/'FALSE'  (older writer)
      * news_feed.csv  -> 'True'/'False'  (the Gemini-era writer)
      * the xlsx       -> numpy bool      (Is_Duplicate)
      * the xlsx       -> float 1.0/0.0   (Mentions_Specific_DC — Excel round-trip)

    Missing stays False. Anything not recognised as truthy is False, never NaN —
    a silent NaN here reads as "no positives" and would flatter every metric.
    """
    if series.dtype == bool:
        return series.fillna(False).astype(bool)

    num = pd.to_numeric(series, errors="coerce")
    if num.notna().any():
        txt = series.fillna("").astype(str).str.strip().str.lower()
        return (num == 1) | txt.eq("true")

    return series.fillna("").astype(str).str.strip().str.lower().eq("true")


def as_nullable_bool(series: pd.Series) -> pd.Series:
    """
    Like as_bool, but preserves 'not stated' as <NA>.

    Used for the human correction columns, where blank means "I did not correct this",
    NOT "I said false". Collapsing that distinction is how an eval harness lies.
    """
    num = pd.to_numeric(series, errors="coerce")
    txt = series.fillna("").astype(str).str.strip().str.lower()

    out = pd.Series(pd.NA, index=series.index, dtype="object")
    out[(num == 1) | txt.eq("true")]  = True
    out[(num == 0) | txt.eq("false")] = False
    return out


def as_score(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")
