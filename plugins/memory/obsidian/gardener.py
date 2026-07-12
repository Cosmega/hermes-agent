"""Le Jardinier — Iris tends the vault while you sleep.

A nightly (or on-demand) pass where Iris reviews what changed in the vault
recently, searches for connections between notes, contradictions, and
dropped threads — then writes a dated **morning briefing** into
``<agent folder>/Briefings/YYYY-MM-DD.md``:

- connections it found between notes (as wikilink pairs)
- contradictions or stale facts it spotted
- commitments and open threads that went quiet
- suggested next actions (as copy-ready ``#iris/task`` blocks)

The gardener never modifies user notes: its only output is the briefing
note (plus whatever the agent legitimately does through its normal tools,
e.g. consolidating its own memory). Local inference makes the nightly pass
free — this is compute that would otherwise idle.

Scheduling: run ``iris obsidian gardener`` from cron/systemd at night, or
rely on the combined ``iris obsidian watch --gardener-hour 5`` loop. A
once-per-day guard (state file) makes any scheduling overlap harmless.

Config (plugins.obsidian-memory):
    gardener_days: 3          # lookback window for "recent" notes
    gardener_max_notes: 40    # cap on notes listed in the context
    inbox_task_timeout: 900   # seconds for the gardening session
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .inbox import _atomic_write, _read_state, _write_state

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".obsidian", ".trash", ".git", ".sync-conflicts"}
_MAX_RESULT_CHARS = 30_000
_FIRST_LINE_CHARS = 120

_GARDENER_PROMPT = """[Iris Gardener] It's your nightly gardening pass over the user's vault.

Recently modified notes ({days}-day window):
{recent_block}

Do this, using obsidian(action='search'/'read') to investigate as needed:
1. CONNECTIONS — pairs of notes that talk about the same thing but aren't \
linked; give each as "[[A]] ↔ [[B]] — why".
2. CONTRADICTIONS / STALE FACTS — places where notes (or your memory) \
disagree; quote both sides briefly.
3. DROPPED THREADS — commitments, plans, or questions that appear in notes \
or your memory but have had no activity; phrase each as a gentle reminder.
4. SUGGESTIONS — up to 3 concrete next actions, each as a ready-to-copy \
task block:
   ```
   #iris/task <the task>
   ```
Also: if your memory contains stale or duplicate entries you noticed, \
consolidate them with the memory tool.

Reply with the briefing in Markdown (sections: Connections, \
Contradictions, Dropped threads, Suggestions). Skip any empty section. \
If the vault was quiet, say so in two lines instead of inventing content. \
No preamble — the reply is saved verbatim as the morning briefing note."""


@dataclass
class GardenerConfig:
    vault_path: Path
    agent_folder: str = "Iris"
    days: int = 3
    max_notes: int = 40
    task_timeout: int = 900
    state_path: Optional[Path] = None

    @classmethod
    def from_plugin_config(cls, cfg: Dict, hermes_home: Optional[str] = None) -> "GardenerConfig":
        vault = Path(str(cfg.get("vault_path", ""))).expanduser()
        state = Path(hermes_home) / "obsidian_inbox_state.json" if hermes_home else None
        return cls(
            vault_path=vault,
            agent_folder=str(cfg.get("folder", "Iris")).strip() or "Iris",
            days=int(cfg.get("gardener_days", 3)),
            max_notes=int(cfg.get("gardener_max_notes", 40)),
            task_timeout=int(cfg.get("inbox_task_timeout", 900)),
            state_path=state,
        )

    @property
    def briefings_dir(self) -> Path:
        return self.vault_path / self.agent_folder / "Briefings"


class Gardener:
    """One nightly pass: gather context → agent session → briefing note."""

    def __init__(
        self,
        config: GardenerConfig,
        run_task: Optional[Callable[[str, int], Tuple[bool, str]]] = None,
    ):
        self.config = config
        if run_task is None:
            from .inbox import _run_oneshot_agent
            run_task = _run_oneshot_agent
        self._run_task = run_task

    # -- Context ---------------------------------------------------------------

    def recent_notes(self, now: Optional[_dt.datetime] = None) -> List[Tuple[str, str]]:
        """(vault-relative path, first heading/line) for recently touched notes."""
        now_ts = (now or _dt.datetime.now()).timestamp()
        cutoff = now_ts - self.config.days * 86400
        briefings = self.config.briefings_dir
        found: List[Tuple[float, Path]] = []
        stack = [self.config.vault_path]
        while stack:
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
                    continue
                if entry.suffix.lower() != ".md":
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                # our own briefings would make the gardener navel-gaze
                if briefings in entry.parents:
                    continue
                found.append((mtime, entry))
        found.sort(reverse=True)
        out = []
        for _, path in found[: self.config.max_notes]:
            first = ""
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line and line != "---" and not line.startswith(("tags:", "created:", "source:")):
                            first = line[:_FIRST_LINE_CHARS]
                            break
            except OSError:
                pass
            out.append((str(path.relative_to(self.config.vault_path)), first))
        return out

    def build_prompt(self, now: Optional[_dt.datetime] = None) -> Optional[str]:
        recent = self.recent_notes(now)
        if not recent:
            return None
        block = "\n".join(f"- [[{rel[:-3]}]] — {first}" if first else f"- [[{rel[:-3]}]]"
                          for rel, first in recent)
        return _GARDENER_PROMPT.format(days=self.config.days, recent_block=block)

    # -- Once-per-day guard ---------------------------------------------------------

    def already_ran_today(self, now: Optional[_dt.datetime] = None) -> bool:
        last = str(_read_state(self.config.state_path).get("gardener_last_run", ""))
        today = (now or _dt.datetime.now()).date().isoformat()
        return last[:10] == today

    def _mark_ran(self, now: Optional[_dt.datetime] = None) -> None:
        stamp = (now or _dt.datetime.now()).replace(microsecond=0).isoformat()
        _write_state(self.config.state_path, {"gardener_last_run": stamp})

    # -- Run ----------------------------------------------------------------------------

    def run(self, force: bool = False, now: Optional[_dt.datetime] = None) -> Optional[Path]:
        """Run the pass; returns the briefing path, or None if skipped."""
        now = now or _dt.datetime.now()
        if not force and self.already_ran_today(now):
            logger.info("Gardener already ran today — skipping (use force to override)")
            return None
        prompt = self.build_prompt(now)
        if prompt is None:
            logger.info("Gardener: no recent notes in the last %s days — skipping", self.config.days)
            self._mark_ran(now)
            return None
        try:
            ok, reply = self._run_task(prompt, self.config.task_timeout)
        except Exception as e:
            ok, reply = False, f"Task runner error: {e}"
        reply = (reply or "").strip()[:_MAX_RESULT_CHARS]
        if not ok or not reply:
            logger.warning("Gardener session failed: %s", reply[:200])
            return None

        self._mark_ran(now)
        path = self.config.briefings_dir / f"{now.date().isoformat()}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():  # forced second run the same day: version the file
            path = self.config.briefings_dir / f"{now.strftime('%Y-%m-%d %H%M')}.md"
        body = (
            "---\n"
            f"created: {now.strftime('%Y-%m-%d %H:%M')}\n"
            "source: iris/gardener\n"
            "tags: [iris/briefing]\n"
            "---\n\n"
            f"# ✦ Morning briefing — {now.date().isoformat()}\n\n"
            f"{reply}\n"
        )
        _atomic_write(path, body)
        return path
