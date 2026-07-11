"""Iris Inbox — the Obsidian vault as a bidirectional command surface.

Write a note anywhere in your vault, tag it ``#iris/task``, and Iris picks
it up, runs the task with its full toolset, and appends the result to the
note itself. The vault (and whatever sync you already use) becomes the
transport: no chat app, no open port, no new interface.

Note protocol
=============

A task note is any markdown file that carries the ``#iris/task`` tag
(inline in the body, or ``iris/task`` in frontmatter ``tags``) and has no
``iris-status`` yet::

    ---
    tags: [iris/task]
    ---
    Compare the three frameworks in [[SSG Research]] and recommend one.

Iris drives a state machine in the note's frontmatter::

    iris-status: pending → running → done | failed | blocked

and appends its answer under a marked heading — the user's own text is
never modified::

    ## ✦ Iris — 2026-07-05 07:12

    <answer, with [[wikilinks]]>

Recurring tasks add ``iris-cron: "M H DoM Mon DoW"`` — the note is re-run
on schedule (``iris-last-run`` tracks the last firing) and each run appends
a new dated section.

Safety
======

- Note content is untrusted text that becomes a prompt: every candidate is
  screened with the shared threat-pattern library (same scanner the memory
  tool uses). A hit marks the note ``blocked`` with the pattern ids instead
  of executing.
- A rate limit (``inbox_max_per_hour``) bounds how many tasks can run per
  hour, whatever appears in the vault.
- Optional ``inbox_folder`` restricts scanning to one subfolder.
- Execution goes through a normal one-shot agent session, so the user's
  ``approvals`` policy applies to dangerous commands.

Config (plugins.obsidian-memory):
    inbox_folder: ""            # vault-relative; "" = whole vault
    inbox_max_per_hour: 6       # rate limit on task executions
    inbox_task_timeout: 900     # seconds per task
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TASK_TAG = "iris/task"
_TAG_RE = re.compile(r"(?:^|[\s>(])#iris/task\b")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_SKIP_DIRS = {".obsidian", ".trash", ".git", ".sync-conflicts"}
_MAX_FILE_BYTES = 262_144
_MAX_SCAN_FILES = 20000
_MAX_PROMPT_CHARS = 12_000
_MAX_RESULT_CHARS = 30_000

RUNNABLE_STATUSES = (None, "", "pending")


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".iris-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Note protocol
# ---------------------------------------------------------------------------

def parse_note(text: str) -> Tuple[Dict, str]:
    """Split a note into (frontmatter dict, body). Tolerant of bad YAML."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        import yaml

        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    return fm, text[m.end():]


def render_note(fm: Dict, body: str) -> str:
    import yaml

    fm_text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False,
                             allow_unicode=True).strip()
    return f"---\n{fm_text}\n---\n{body}"


def is_task_note(fm: Dict, body: str) -> bool:
    """Does this note carry the task tag (body inline or frontmatter tags)?"""
    if _TAG_RE.search(body):
        return True
    tags = fm.get("tags")
    if isinstance(tags, str):
        tags = [t.strip().lstrip("#") for t in tags.split(",")]
    if isinstance(tags, list):
        return any(str(t).strip().lstrip("#") == TASK_TAG for t in tags)
    return False


def _cron_due(fm: Dict, now: _dt.datetime) -> bool:
    """For iris-cron notes: is a new run due since iris-last-run?"""
    expr = str(fm.get("iris-cron") or "").strip()
    if not expr:
        return False
    try:
        from croniter import croniter

        last_raw = str(fm.get("iris-last-run") or "").strip()
        if not last_raw:
            return True  # never ran
        last = _dt.datetime.fromisoformat(last_raw)
        next_fire = croniter(expr, last).get_next(_dt.datetime)
        return next_fire <= now
    except Exception as e:
        logger.debug("Invalid iris-cron %r: %s", expr, e)
        return False


def note_is_runnable(fm: Dict, body: str, now: Optional[_dt.datetime] = None) -> bool:
    """One-shot notes run while status is pending; cron notes run when due."""
    if not is_task_note(fm, body):
        return False
    now = now or _now()
    if fm.get("iris-cron"):
        status = fm.get("iris-status")
        if status == "blocked":
            return False
        return _cron_due(fm, now)
    return fm.get("iris-status") in RUNNABLE_STATUSES


def build_prompt(rel_path: str, body: str) -> str:
    """Turn the note body into the task prompt for the agent."""
    task = _TAG_RE.sub(" ", body).strip()
    task = task[:_MAX_PROMPT_CHARS]
    return (
        f"[Iris Inbox] The user left you this task in their Obsidian vault, "
        f"in the note '{rel_path}'. Complete it now and reply with the final "
        f"answer in Markdown (it will be appended to that note — use "
        f"[[wikilinks]] to reference vault notes, and keep it self-contained):"
        f"\n\n{task}"
    )


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

@dataclass
class InboxConfig:
    vault_path: Path
    folder: str = ""                 # vault-relative scan restriction ("" = whole vault)
    max_per_hour: int = 6
    task_timeout: int = 900
    state_path: Optional[Path] = None  # rate-limit ledger (under HERMES_HOME)

    @classmethod
    def from_plugin_config(cls, cfg: Dict, hermes_home: Optional[str] = None) -> "InboxConfig":
        vault = Path(str(cfg.get("vault_path", ""))).expanduser()
        state = Path(hermes_home) / "obsidian_inbox_state.json" if hermes_home else None
        return cls(
            vault_path=vault,
            folder=str(cfg.get("inbox_folder", "") or "").strip().strip("/"),
            max_per_hour=int(cfg.get("inbox_max_per_hour", 6)),
            task_timeout=int(cfg.get("inbox_task_timeout", 900)),
            state_path=state,
        )


@dataclass
class InboxResult:
    scanned: int = 0
    ran: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    rate_limited: int = 0


class InboxWatcher:
    """Scans the vault for runnable task notes and executes them."""

    def __init__(
        self,
        config: InboxConfig,
        run_task: Optional[Callable[[str, int], Tuple[bool, str]]] = None,
    ):
        self.config = config
        # Injectable executor: (prompt, timeout) -> (ok, result_markdown)
        self._run_task = run_task or _run_oneshot_agent

    # -- Scanning -------------------------------------------------------------

    def _scan_root(self) -> Path:
        root = self.config.vault_path
        if self.config.folder:
            root = root / self.config.folder
        return root

    def _iter_md(self):
        root = self._scan_root()
        if not root.is_dir():
            return
        count = 0
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.suffix.lower() == ".md":
                    yield entry
                    count += 1
                    if count >= _MAX_SCAN_FILES:
                        return

    def find_runnable(self, now: Optional[_dt.datetime] = None) -> List[Path]:
        found = []
        for path in self._iter_md():
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm, body = parse_note(text)
            if note_is_runnable(fm, body, now):
                found.append(path)
        return found

    # -- Rate limiting -----------------------------------------------------------

    def _load_ledger(self) -> List[float]:
        p = self.config.state_path
        if not p or not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return [float(t) for t in data.get("runs", [])]
        except Exception:
            return []

    def _record_run(self, ledger: List[float]) -> None:
        p = self.config.state_path
        ledger.append(time.time())
        if p:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(p, json.dumps({"runs": ledger[-100:]}))
            except OSError:
                pass

    def _allowance(self, ledger: List[float]) -> int:
        cutoff = time.time() - 3600
        recent = [t for t in ledger if t > cutoff]
        return max(0, self.config.max_per_hour - len(recent))

    # -- Note updates --------------------------------------------------------------

    @staticmethod
    def _update_note(path: Path, fm_updates: Dict, append: str = "") -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        fm, body = parse_note(text)
        fm.update(fm_updates)
        if append:
            body = body.rstrip() + "\n\n" + append.strip() + "\n"
        _atomic_write(path, render_note(fm, body))

    # -- Execution --------------------------------------------------------------------

    def process_note(self, path: Path) -> str:
        """Run one task note through the full lifecycle. Returns final status."""
        rel = str(path.relative_to(self.config.vault_path))
        text = path.read_text(encoding="utf-8", errors="replace")
        fm, body = parse_note(text)
        now = _now()
        stamp = now.strftime("%Y-%m-%d %H:%M")

        # Untrusted note text is about to become a prompt — screen it first.
        try:
            from tools.threat_patterns import scan_for_threats

            hits = scan_for_threats(body, scope="context")
        except Exception:
            hits = []
        if hits:
            self._update_note(path, {
                "iris-status": "blocked",
                "iris-blocked-reason": f"threat patterns: {', '.join(hits[:5])}",
            })
            logger.warning("Inbox note %s blocked (threat patterns: %s)", rel, hits)
            return "blocked"

        self._update_note(path, {"iris-status": "running", "iris-started": stamp})
        prompt = build_prompt(rel, body)
        try:
            ok, result = self._run_task(prompt, self.config.task_timeout)
        except Exception as e:
            ok, result = False, f"Task runner error: {e}"
        result = (result or "").strip()[:_MAX_RESULT_CHARS] or "_(no output)_"

        done_stamp = _now().strftime("%Y-%m-%d %H:%M")
        updates: Dict = {"iris-status": "done" if ok else "failed"}
        if fm.get("iris-cron"):
            # Cron notes go back to a waiting state and keep firing.
            updates = {"iris-status": "done" if ok else "failed",
                       "iris-last-run": _now().replace(microsecond=0).isoformat()}
        section = f"## ✦ Iris — {done_stamp}\n\n{result}"
        if not ok:
            section = (f"## ✦ Iris — {done_stamp} (failed)\n\n{result}\n\n"
                       f"_Fix the note or remove `iris-status` to retry._")
        self._update_note(path, updates, append=section)
        return "done" if ok else "failed"

    def run_once(self, now: Optional[_dt.datetime] = None) -> InboxResult:
        """One scan-and-process pass."""
        result = InboxResult()
        candidates = self.find_runnable(now)
        result.scanned = len(candidates)
        if not candidates:
            return result
        ledger = self._load_ledger()
        for path in candidates:
            if self._allowance(ledger) <= 0:
                result.rate_limited += 1
                continue
            self._record_run(ledger)
            status = self.process_note(path)
            rel = str(path.relative_to(self.config.vault_path))
            if status == "done":
                result.ran.append(rel)
            elif status == "blocked":
                result.blocked.append(rel)
            else:
                result.failed.append(rel)
        return result

    def run_forever(self, interval: float = 30.0) -> None:
        logger.info("Iris Inbox watching %s (every %.0fs)", self._scan_root(), interval)
        while True:
            try:
                res = self.run_once()
                for rel in res.ran:
                    print(f"✦ done: {rel}")
                for rel in res.failed:
                    print(f"✗ failed: {rel}")
                for rel in res.blocked:
                    print(f"⛔ blocked: {rel}")
                if res.rate_limited:
                    print(f"⏳ rate-limited: {res.rate_limited} task(s) deferred")
            except Exception as e:
                logger.warning("Inbox pass failed: %s", e)
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Default executor: one-shot agent session in a subprocess
# ---------------------------------------------------------------------------

def _find_cli() -> List[str]:
    """Locate the iris/hermes CLI, preferring the current interpreter's venv."""
    bindir = Path(sys.executable).parent
    for name in ("iris", "hermes"):
        candidate = bindir / name
        if candidate.exists():
            return [str(candidate)]
    return ["iris"]  # PATH fallback


def _run_oneshot_agent(prompt: str, timeout: int) -> Tuple[bool, str]:
    """Run the task as a one-shot agent session; returns (ok, markdown)."""
    cmd = _find_cli() + ["-z", prompt, "--cli"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s."
    out = _ANSI_RE.sub("", proc.stdout or "").strip()
    if proc.returncode != 0:
        err = _ANSI_RE.sub("", (proc.stderr or ""))[-2000:]
        return False, out or f"Agent exited with code {proc.returncode}.\n```\n{err}\n```"
    # One-shot sessions print infrastructure failures as their final message
    # and still exit 0 — treat the known signatures as task failures so the
    # note is marked failed (and retryable) instead of done.
    lowered = out.lower()
    if not out or any(sig in lowered for sig in (
        "api call failed after", "failed to initialize agent",
        "no api key", "provider not configured",
    )):
        return False, out or "_(empty agent output)_"
    return True, out
