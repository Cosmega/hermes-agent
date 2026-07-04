"""Incremental SQLite FTS5 index over an Obsidian vault.

Keeps a full-text index of every ``.md`` file in the vault so search and
prefetch stay fast on vaults far larger than the bounded scan could cover.
The index lives OUTSIDE the vault (under HERMES_HOME) so it never shows up
in Obsidian or vault sync.

Refresh is incremental: a cheap stat() walk finds files whose (mtime, size)
changed since the last pass; only those are re-read. Refreshes are throttled
by a TTL so per-turn prefetch doesn't re-walk the vault needlessly.

If this SQLite build lacks FTS5, ``available`` is False and the provider
falls back to its bounded scan.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_MAX_INDEX_FILES = 20000
_MAX_FILE_BYTES = 262_144  # skip anything bigger, same cap as the scan path
_REFRESH_TTL_SECONDS = 20.0
_SKIP_DIRS = {".obsidian", ".trash", ".git", ".sync-conflicts"}


class VaultIndex:
    """FTS5-backed search index for a vault's markdown files."""

    def __init__(self, db_path: str, vault: Path):
        self._vault = vault
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self.available = False
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS files ("
                "path TEXT PRIMARY KEY, mtime REAL, size INTEGER)"
            )
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes "
                "USING fts5(path UNINDEXED, title, body)"
            )
            self._conn.commit()
            self.available = True
        except sqlite3.Error as e:
            logger.debug("Vault index unavailable (no FTS5?): %s", e)

    def close(self) -> None:
        if self.available:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self.available = False

    # -- Refresh ---------------------------------------------------------------

    def _walk(self) -> Dict[str, tuple]:
        """Return {vault-relative path: (mtime, size)} for all markdown files."""
        found: Dict[str, tuple] = {}
        stack = [self._vault]
        while stack and len(found) < _MAX_INDEX_FILES:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.suffix.lower() == ".md":
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    if st.st_size > _MAX_FILE_BYTES:
                        continue
                    rel = str(entry.relative_to(self._vault))
                    found[rel] = (st.st_mtime, st.st_size)
        return found

    def refresh(self, force: bool = False) -> None:
        """Bring the index up to date with the vault. TTL-throttled."""
        if not self.available:
            return
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_refresh < _REFRESH_TTL_SECONDS:
                return
            self._last_refresh = now
            try:
                on_disk = self._walk()
                indexed = {
                    row[0]: (row[1], row[2])
                    for row in self._conn.execute("SELECT path, mtime, size FROM files")
                }
                stale = [p for p in indexed if p not in on_disk]
                changed = [
                    p for p, sig in on_disk.items()
                    if indexed.get(p) != sig
                ]
                for path in stale:
                    self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
                    self._conn.execute("DELETE FROM notes WHERE path = ?", (path,))
                for rel in changed:
                    try:
                        text = (self._vault / rel).read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        continue
                    mtime, size = on_disk[rel]
                    self._conn.execute(
                        "INSERT OR REPLACE INTO files (path, mtime, size) VALUES (?, ?, ?)",
                        (rel, mtime, size),
                    )
                    self._conn.execute("DELETE FROM notes WHERE path = ?", (rel,))
                    self._conn.execute(
                        "INSERT INTO notes (path, title, body) VALUES (?, ?, ?)",
                        (rel, Path(rel).stem, text),
                    )
                self._conn.commit()
            except sqlite3.Error as e:
                logger.debug("Vault index refresh failed: %s", e)

    def upsert(self, rel_path: str) -> None:
        """Index a single file immediately (bypasses the refresh TTL).

        Called after the provider writes a note so the agent's own writes are
        searchable in the same turn instead of after the next TTL refresh.
        """
        if not self.available:
            return
        full = self._vault / rel_path
        with self._lock:
            try:
                if not full.exists():
                    self._conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
                    self._conn.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
                    self._conn.commit()
                    return
                st = full.stat()
                if st.st_size > _MAX_FILE_BYTES:
                    return
                text = full.read_text(encoding="utf-8", errors="replace")
                self._conn.execute(
                    "INSERT OR REPLACE INTO files (path, mtime, size) VALUES (?, ?, ?)",
                    (rel_path, st.st_mtime, st.st_size),
                )
                self._conn.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
                self._conn.execute(
                    "INSERT INTO notes (path, title, body) VALUES (?, ?, ?)",
                    (rel_path, Path(rel_path).stem, text),
                )
                self._conn.commit()
            except (sqlite3.Error, OSError) as e:
                logger.debug("Vault index upsert failed for %s: %s", rel_path, e)

    # -- Queries ----------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        """Full-text search. Returns [{path, score, snippet}] best-first."""
        if not self.available:
            return []
        words = re.findall(r"\w{3,}", query.lower())[:12]
        if not words:
            return []
        fts_query = " OR ".join(f'"{w}"' for w in words)
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT path, bm25(notes, 0, 5.0, 1.0) AS rank, "
                    "snippet(notes, 2, '', '', '…', 24) "
                    "FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit),
                ).fetchall()
            except sqlite3.Error as e:
                logger.debug("Vault index search failed: %s", e)
                return []
        return [
            {"path": path, "score": round(-rank, 2), "snippet": snippet.strip()}
            for path, rank, snippet in rows
        ]

    def titles(self, max_titles: int = 2000) -> Dict[str, str]:
        """Return {note title (stem): vault-relative path} for auto-linking."""
        if not self.available:
            return {}
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT path FROM files LIMIT ?", (max_titles,)
                ).fetchall()
            except sqlite3.Error:
                return {}
        return {Path(row[0]).stem: row[0] for row in rows}
