"""La Dépose — a drop folder where anything becomes knowledge.

Throw any file into ``<agent folder>/Drop/`` in the vault — a PDF, a
screenshot, an article, a voice memo, a text snippet. The watcher detects
it, an agent session reads/analyzes it (vision for images, transcription
for audio, plain reading for text), and the reply becomes a wikilinked
Markdown note in ``<agent folder>/Notes/``. The original moves to
``Drop/Archive/`` and the run is logged in ``Drop/Journal.md``.

Combined with the phone's "share → synced folder", the entire incoming
stream (tickets, articles, dictated ideas) turns into structured notes
with zero manual filing.

Failures leave the file in place with exponential-ish backoff (skip if it
failed recently, give up after ``_MAX_ATTEMPTS``) so one broken PDF can't
wedge the queue. Executions draw from the same per-hour rate-limit budget
as Iris Inbox.

Config (plugins.obsidian-memory):
    drop_folder: ""             # vault-relative; "" = "<folder>/Drop"
    inbox_max_per_hour: 6       # shared budget with the inbox
    inbox_task_timeout: 900     # seconds per file
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .inbox import (
    _atomic_write,
    _read_state,
    _write_state,
    allowance,
    load_ledger,
    record_run,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_COOLDOWN_S = 3600           # wait an hour before retrying a failed file
_MAX_INLINE_BYTES = 16_384         # inline small text files into the prompt
_MAX_RESULT_CHARS = 30_000
_TEXT_SUFFIXES = {".txt", ".md", ".html", ".htm", ".csv", ".json", ".log"}
_IGNORED_SUFFIXES = {".tmp", ".part", ".crdownload", ".sync-conflict"}


def _title_from_filename(name: str) -> str:
    stem = Path(name).stem.replace("_", " ").replace("-", " ").strip()
    return (stem[:1].upper() + stem[1:])[:120] or "Dropped file"


def build_drop_prompt(file_path: Path, rel_notes_dir: str) -> str:
    """Prompt for one dropped file. Small text files are inlined; binaries
    are referenced by path so the agent uses its tools (vision, reading,
    transcription) on them."""
    suffix = file_path.suffix.lower()
    inline = ""
    if suffix in _TEXT_SUFFIXES:
        try:
            if file_path.stat().st_size <= _MAX_INLINE_BYTES:
                inline = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    header = (
        f"[Iris Drop] The user dropped the file '{file_path.name}' into their "
        f"vault's drop folder. Analyze it and reply with a well-structured "
        f"Markdown NOTE BODY (no frontmatter) that will be saved into the "
        f"vault at '{rel_notes_dir}/': a short summary first, then the key "
        f"points, using [[wikilinks]] for people/projects/topics that likely "
        f"exist as notes, and #tags for categories. Reply with the note body "
        f"only — no preamble."
    )
    if inline:
        return f"{header}\n\nFile content:\n\n{inline}"
    return (
        f"{header}\n\nThe file is on disk at: {file_path}\n"
        f"Use your tools to read it (read_file for text/PDF, vision_analyze "
        f"for images, transcription for audio) before writing the note."
    )


@dataclass
class DropConfig:
    vault_path: Path
    agent_folder: str = "Iris"
    drop_folder: str = ""            # vault-relative; "" = "<agent_folder>/Drop"
    max_per_hour: int = 6
    task_timeout: int = 900
    state_path: Optional[Path] = None

    @classmethod
    def from_plugin_config(cls, cfg: Dict, hermes_home: Optional[str] = None) -> "DropConfig":
        vault = Path(str(cfg.get("vault_path", ""))).expanduser()
        state = Path(hermes_home) / "obsidian_inbox_state.json" if hermes_home else None
        return cls(
            vault_path=vault,
            agent_folder=str(cfg.get("folder", "Iris")).strip() or "Iris",
            drop_folder=str(cfg.get("drop_folder", "") or "").strip().strip("/"),
            max_per_hour=int(cfg.get("inbox_max_per_hour", 6)),
            task_timeout=int(cfg.get("inbox_task_timeout", 900)),
            state_path=state,
        )

    @property
    def drop_dir(self) -> Path:
        rel = self.drop_folder or f"{self.agent_folder}/Drop"
        return self.vault_path / rel

    @property
    def archive_dir(self) -> Path:
        return self.drop_dir / "Archive"

    @property
    def notes_dir(self) -> Path:
        return self.vault_path / self.agent_folder / "Notes"

    @property
    def journal_path(self) -> Path:
        return self.drop_dir / "Journal.md"


@dataclass
class DropResult:
    processed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: int = 0
    rate_limited: int = 0


class DropProcessor:
    """Turns files dropped in the vault into wikilinked notes."""

    def __init__(
        self,
        config: DropConfig,
        run_task: Optional[Callable[[str, int], Tuple[bool, str]]] = None,
    ):
        self.config = config
        if run_task is None:
            from .inbox import _run_oneshot_agent
            run_task = _run_oneshot_agent
        self._run_task = run_task

    # -- Discovery -----------------------------------------------------------

    def find_pending(self) -> List[Path]:
        d = self.config.drop_dir
        if not d.is_dir():
            return []
        attempts = self._attempts()
        now = time.time()
        pending = []
        for entry in sorted(d.iterdir()):
            if entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name == "Journal.md":
                continue
            if entry.suffix.lower() in _IGNORED_SUFFIXES:
                continue
            tries = attempts.get(entry.name, [])
            if len(tries) >= _MAX_ATTEMPTS:
                continue
            if tries and now - tries[-1] < _RETRY_COOLDOWN_S:
                continue
            pending.append(entry)
        return pending

    # -- Attempt tracking (backoff so a broken file can't wedge the queue) ----

    def _attempts(self) -> Dict[str, List[float]]:
        raw = _read_state(self.config.state_path).get("drop_attempts", {})
        return {k: [float(t) for t in v] for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _record_attempt(self, name: str) -> None:
        attempts = self._attempts()
        attempts.setdefault(name, []).append(time.time())
        # keep the ledger from growing without bound
        trimmed = {k: v[-_MAX_ATTEMPTS:] for k, v in list(attempts.items())[-200:]}
        _write_state(self.config.state_path, {"drop_attempts": trimmed})

    def _clear_attempts(self, name: str) -> None:
        attempts = self._attempts()
        if attempts.pop(name, None) is not None:
            _write_state(self.config.state_path, {"drop_attempts": attempts})

    # -- Processing ---------------------------------------------------------------

    def _unique_note_path(self, title: str) -> Path:
        base = self.config.notes_dir / f"{title}.md"
        if not base.exists():
            return base
        for i in range(2, 100):
            candidate = self.config.notes_dir / f"{title} {i}.md"
            if not candidate.exists():
                return candidate
        return self.config.notes_dir / f"{title} {int(time.time())}.md"

    def _journal(self, line: str) -> None:
        p = self.config.journal_path
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            existing = ""
            if p.exists():
                existing = p.read_text(encoding="utf-8", errors="replace").rstrip()
            else:
                existing = "# Drop Journal\n"
            _atomic_write(p, existing + f"\n- {stamp} — {line}\n")
        except OSError:
            pass

    def process_file(self, path: Path) -> bool:
        """One file through the full lifecycle. Returns success."""
        rel_notes = str(self.config.notes_dir.relative_to(self.config.vault_path))
        prompt = build_drop_prompt(path, rel_notes)
        self._record_attempt(path.name)
        try:
            ok, reply = self._run_task(prompt, self.config.task_timeout)
        except Exception as e:
            ok, reply = False, f"Task runner error: {e}"
        reply = (reply or "").strip()[:_MAX_RESULT_CHARS]
        if not ok or not reply:
            self._journal(f"✗ {path.name} — failed: {(reply or 'no output')[:200]}")
            return False

        title = _title_from_filename(path.name)
        note_path = self._unique_note_path(title)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        body = (
            "---\n"
            f"created: {stamp}\n"
            "source: iris/drop\n"
            f"original: {path.name}\n"
            "tags: [iris/drop]\n"
            "---\n\n"
            f"{reply}\n"
        )
        _atomic_write(note_path, body)

        self.config.archive_dir.mkdir(parents=True, exist_ok=True)
        archived = self.config.archive_dir / path.name
        if archived.exists():
            archived = self.config.archive_dir / f"{int(time.time())}-{path.name}"
        try:
            path.rename(archived)
        except OSError as e:
            logger.warning("Could not archive %s: %s", path.name, e)

        rel_note = note_path.relative_to(self.config.vault_path)
        link = str(rel_note)[:-3]
        self._journal(f"✦ {path.name} → [[{link}]]")
        self._clear_attempts(path.name)
        return True

    def run_once(self) -> DropResult:
        result = DropResult()
        pending = self.find_pending()
        if not pending:
            return result
        ledger = load_ledger(self.config.state_path)
        for path in pending:
            if allowance(ledger, self.config.max_per_hour) <= 0:
                result.rate_limited += 1
                continue
            record_run(self.config.state_path, ledger)
            if self.process_file(path):
                result.processed.append(path.name)
            else:
                result.failed.append(path.name)
        return result
