"""
LightSignal — _staging.py
===========================
Shared read/write helpers for staged_articles.csv.

These four lines of CSV plumbing previously existed as four separate copies in
extract.py, dedup.py, summarize.py and classify.py. They had already drifted:
only summarize.py's copy tolerated rows with differing key sets, so adding a
column anywhere else raised ValueError from DictWriter. One copy now.
"""

import csv
from pathlib import Path


def load_staging(path: Path) -> list:
    """Load all rows from a staging CSV. Returns [] if it does not exist."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_staging(path: Path, rows: list) -> None:
    """
    Write all rows back to a staging CSV.

    Field names are the union of every row's keys in first-seen order. Using
    rows[0].keys() alone breaks the moment one stage adds a column to some rows
    but not others (e.g. summarize_attempts).
    """
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    seen = set(fieldnames)
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
