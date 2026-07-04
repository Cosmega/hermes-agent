"""Obsidian vault memory plugin using the MemoryProvider interface.

Persists agent memory as plain Markdown notes inside a local Obsidian
vault. Everything is local-first: no server, no API key, no network.
The vault stays a normal Obsidian vault — notes are readable, editable,
linkable, and sync however the user already syncs their vault.

Vault layout (all under the configured subfolder, default "Iris"):
  <folder>/Memory.md           — mirror of the built-in MEMORY.md
  <folder>/User Profile.md     — mirror of the built-in USER.md
  <folder>/Notes/<Title>.md    — notes the agent creates via the tool
  <folder>/Sessions/<date>.md  — end-of-session notes (LLM summary when
                                 available, digest otherwise)

Write scope: the agent can READ and SEARCH the whole vault, but can only
WRITE and DELETE inside the configured subfolder. User notes are never
modified — the single opt-in exception is ``daily_notes``, which appends
a clearly-marked session section to the day's daily note.

Search is backed by an incremental SQLite FTS5 index stored under
HERMES_HOME (never inside the vault); it falls back to a bounded text
scan when FTS5 is unavailable.

Config in $HERMES_HOME/config.yaml:
  plugins:
    obsidian-memory:
      vault_path: ~/Documents/MyVault   # required — path to the vault
      folder: Iris                      # subfolder the agent writes into
      session_notes: true               # write a note at session end
      session_summary: auto             # auto = LLM summary w/ digest fallback, off = digest
      daily_notes: false                # append session section to the daily note
      daily_notes_folder: ""            # vault-relative daily notes folder ("" = root)
      daily_note_format: "%Y-%m-%d"     # daily note filename date format
      auto_link: true                   # wikilink existing note titles in written notes
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from hermes_cli.config import cfg_get
from tools.registry import tool_error

from .index import VaultIndex

logger = logging.getLogger(__name__)

# Bounds for the fallback vault scan (used only when FTS5 is unavailable).
_MAX_SCAN_FILES = 500
_MAX_FILE_BYTES = 262_144  # 256 KB — skip anything bigger
_SCAN_BUDGET_SECONDS = 1.5
_SKIP_DIRS = {".obsidian", ".trash", ".git", ".sync-conflicts"}

# Auto-linking limits: don't turn a note into link soup.
_AUTO_LINK_MAX = 8
_AUTO_LINK_MIN_TITLE_LEN = 4

# Characters Obsidian forbids in note titles, plus path separators.
_TITLE_SANITIZE_RE = re.compile(r'[\\/:*?"<>|#^\[\]]')

_SUMMARY_SYSTEM_PROMPT = (
    "You summarize a conversation between a user and their assistant Iris "
    "into a compact Obsidian note. Reply with Markdown only: 3-6 bullet "
    "points covering decisions, facts learned, and follow-ups. No preamble, "
    "no heading, no code fences."
)


OBSIDIAN_TOOL_SCHEMA = {
    "name": "obsidian",
    "description": (
        "Your long-term memory, stored as Markdown notes in the user's Obsidian vault. "
        "Notes persist across sessions and are visible to the user inside Obsidian.\n\n"
        "ACTIONS:\n"
        "• search — Find notes anywhere in the vault matching keywords. ALWAYS search "
        "before answering questions about past work, people, projects, or preferences.\n"
        "• read — Read a note by vault-relative path.\n"
        "• list — List notes (whole vault or a subfolder).\n"
        "• write — Create or overwrite a note in your memory folder. Use [[wikilinks]] "
        "to connect related notes and #tags for categories.\n"
        "• append — Add to an existing note in your memory folder (creates it if missing).\n"
        "• delete — Delete a note you created in your memory folder.\n\n"
        "Writes are restricted to your memory folder — the rest of the vault is read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "read", "list", "write", "append", "delete"],
            },
            "query": {"type": "string", "description": "Keywords for 'search'."},
            "path": {
                "type": "string",
                "description": "Vault-relative note path for 'read'/'delete', or folder for 'list'.",
            },
            "title": {"type": "string", "description": "Note title for 'write'/'append'."},
            "content": {"type": "string", "description": "Markdown content for 'write'/'append'."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Frontmatter tags for 'write' (e.g. ['project', 'preference']).",
            },
            "limit": {"type": "integer", "description": "Max results for 'search'/'list' (default 10)."},
        },
        "required": ["action"],
    },
}


def _load_plugin_config() -> dict:
    from hermes_constants import get_hermes_home

    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml

        with open(config_path, encoding="utf-8-sig") as f:
            all_config = yaml.safe_load(f) or {}
        return cfg_get(all_config, "plugins", "obsidian-memory", default={}) or {}
    except Exception:
        return {}


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    if value is None:
        return default
    return bool(value)


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + rename so vault sync never sees a half-written note."""
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


class ObsidianMemoryProvider(MemoryProvider):
    """Markdown-note memory backed by a local Obsidian vault."""

    def __init__(self, config: dict | None = None):
        self._config = config if config is not None else _load_plugin_config()
        self._vault: Optional[Path] = None
        self._folder_name = str(self._config.get("folder", "Iris")).strip() or "Iris"
        self._session_notes = _as_bool(self._config.get("session_notes", True))
        self._session_summary = str(self._config.get("session_summary", "auto")).strip().lower()
        self._daily_notes = _as_bool(self._config.get("daily_notes", False), default=False)
        self._daily_notes_folder = str(self._config.get("daily_notes_folder", "")).strip().strip("/")
        self._daily_note_format = str(self._config.get("daily_note_format", "%Y-%m-%d"))
        self._auto_link_enabled = _as_bool(self._config.get("auto_link", True))
        self._session_id = ""
        self._write_enabled = True
        self._write_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._index: Optional[VaultIndex] = None

    @property
    def name(self) -> str:
        return "obsidian"

    # -- Availability / config ------------------------------------------------

    def _resolve_vault_path(self) -> Optional[Path]:
        raw = self._config.get("vault_path", "")
        if not raw:
            return None
        try:
            path = Path(str(raw)).expanduser()
            return path if path.is_dir() else None
        except Exception:
            return None

    def is_available(self) -> bool:
        return self._resolve_vault_path() is not None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "vault_path",
                "description": "Path to your Obsidian vault (the folder Obsidian opens)",
                "required": True,
            },
            {
                "key": "folder",
                "description": "Subfolder inside the vault where the agent writes its notes",
                "default": "Iris",
            },
            {
                "key": "session_notes",
                "description": "Write a conversation note at the end of each session",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "session_summary",
                "description": "Summarize sessions with the LLM (falls back to a digest)",
                "default": "auto",
                "choices": ["auto", "off"],
            },
            {
                "key": "daily_notes",
                "description": "Also append a session section to your Obsidian daily note",
                "default": "false",
                "choices": ["true", "false"],
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml

            existing = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or {}
            existing.setdefault("plugins", {})
            existing["plugins"]["obsidian-memory"] = values
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False)
            self._config.update(values)
        except Exception as e:
            logger.warning("Failed to save obsidian-memory config: %s", e)

    # -- Lifecycle -------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._vault = self._resolve_vault_path()
        if not self._vault:
            raise RuntimeError(
                "Obsidian memory: vault_path is not configured or does not exist. "
                "Run 'hermes memory setup' and select 'obsidian'."
            )
        # Cron/subagent contexts must not spam the vault with session notes.
        agent_context = kwargs.get("agent_context", "primary")
        self._write_enabled = agent_context == "primary"
        self._hermes_dir.mkdir(parents=True, exist_ok=True)
        # The FTS index lives under HERMES_HOME, never inside the vault.
        hermes_home = kwargs.get("hermes_home")
        if not hermes_home:
            try:
                from hermes_constants import get_hermes_home
                hermes_home = str(get_hermes_home())
            except Exception:
                hermes_home = None
        if hermes_home:
            try:
                index = VaultIndex(str(Path(hermes_home) / "obsidian_index.db"), self._vault)
                self._index = index if index.available else None
            except Exception as e:
                logger.debug("Vault index init failed, using scan fallback: %s", e)
                self._index = None

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id

    def shutdown(self) -> None:
        if self._index:
            self._index.close()
            self._index = None
        self._vault = None

    # -- Paths -----------------------------------------------------------------

    @property
    def _hermes_dir(self) -> Path:
        return self._vault / self._folder_name

    @staticmethod
    def _sanitize_title(title: str) -> str:
        clean = _TITLE_SANITIZE_RE.sub(" ", title)
        clean = re.sub(r"\s+", " ", clean).strip(" .")
        return clean[:120] or "Untitled"

    def _resolve_in_vault(self, rel_path: str) -> Path:
        """Resolve a vault-relative path, refusing escapes outside the vault."""
        candidate = (self._vault / rel_path.lstrip("/")).resolve()
        vault = self._vault.resolve()
        if candidate != vault and vault not in candidate.parents:
            raise ValueError("path escapes the vault")
        return candidate

    def _resolve_in_hermes_dir(self, rel_path: str) -> Path:
        """Resolve a path and require it to be inside the agent's folder."""
        candidate = self._resolve_in_vault(rel_path)
        hermes = self._hermes_dir.resolve()
        if candidate != hermes and hermes not in candidate.parents:
            raise ValueError(
                f"writes are restricted to the '{self._folder_name}/' folder of the vault"
            )
        return candidate

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._vault.resolve()))
        except ValueError:
            return str(path)

    def _index_note(self, path: Path) -> None:
        """Reflect one of our own writes/deletes in the search index right away."""
        if self._index:
            self._index.upsert(self._rel(path))

    # -- System prompt / prefetch ------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._vault:
            return ""
        try:
            note_count = sum(1 for _ in self._iter_vault_files(limit=_MAX_SCAN_FILES))
        except Exception:
            note_count = 0
        return (
            "# Obsidian Memory\n"
            f"Active. Your memory lives as Markdown notes in the user's Obsidian vault "
            f"({note_count}+ notes readable). Your writable folder is "
            f"'{self._folder_name}/'.\n"
            "Use obsidian(action='search') before answering questions about the user, "
            "their projects, or past decisions. Persist durable knowledge with "
            "obsidian(action='write'/'append') using [[wikilinks]] and #tags so the "
            "user's knowledge graph grows."
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._vault or not query:
            return

        def _run() -> None:
            try:
                result = self._format_search_context(query)
            except Exception as e:
                logger.debug("Obsidian prefetch failed: %s", e)
                result = ""
            with self._prefetch_lock:
                self._prefetch_query = query
                self._prefetch_result = result

        threading.Thread(target=_run, daemon=True, name="obsidian-prefetch").start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._vault or not query:
            return ""
        with self._prefetch_lock:
            if self._prefetch_query == query and self._prefetch_result:
                return self._prefetch_result
        # No cached background result — do a bounded synchronous search.
        try:
            return self._format_search_context(query)
        except Exception as e:
            logger.debug("Obsidian prefetch search failed: %s", e)
            return ""

    def _format_search_context(self, query: str) -> str:
        hits = self._search_vault(query, limit=3)
        if not hits:
            return ""
        lines = ["## Obsidian Memory (relevant notes)"]
        for hit in hits:
            link = hit["path"][:-3] if hit["path"].endswith(".md") else hit["path"]
            lines.append(f"### [[{link}]]")
            lines.append(hit["snippet"])
        return "\n".join(lines)

    # -- Tools -------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [OBSIDIAN_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "obsidian":
            return tool_error(f"Unknown tool: {tool_name}")
        if not self._vault:
            return tool_error("Obsidian memory is not initialized")
        try:
            action = args["action"]
            if action == "search":
                return self._tool_search(args)
            if action == "read":
                return self._tool_read(args)
            if action == "list":
                return self._tool_list(args)
            if action == "write":
                return self._tool_write(args, overwrite=True)
            if action == "append":
                return self._tool_write(args, overwrite=False)
            if action == "delete":
                return self._tool_delete(args)
            return tool_error(f"Unknown action: {action}")
        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except ValueError as exc:
            return tool_error(str(exc))
        except OSError as exc:
            return tool_error(f"Vault I/O error: {exc}")

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("search requires 'query'")
        limit = max(1, min(int(args.get("limit", 10)), 25))
        hits = self._search_vault(query, limit=limit)
        return json.dumps({"results": hits, "count": len(hits)})

    def _tool_read(self, args: dict) -> str:
        rel = args.get("path", "").strip()
        if not rel:
            return tool_error("read requires 'path'")
        path = self._resolve_in_vault(rel)
        if path.is_dir():
            return tool_error(f"'{rel}' is a folder — use action='list'")
        if not path.exists() and not rel.endswith(".md"):
            path = self._resolve_in_vault(rel + ".md")
        if not path.exists():
            return tool_error(f"Note not found: {rel}")
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > 40_000:
            content = content[:40_000] + "\n… (truncated)"
        return json.dumps({"path": self._rel(path), "content": content})

    def _tool_list(self, args: dict) -> str:
        rel = args.get("path", "").strip()
        base = self._resolve_in_vault(rel) if rel else self._vault
        if not base.is_dir():
            return tool_error(f"Folder not found: {rel}")
        limit = max(1, min(int(args.get("limit", 50)), 200))
        notes = []
        for path in self._iter_vault_files(root=base, limit=limit):
            notes.append(self._rel(path))
        return json.dumps({"notes": sorted(notes), "count": len(notes)})

    def _tool_write(self, args: dict, *, overwrite: bool) -> str:
        title = self._sanitize_title(args.get("title", ""))
        content = args.get("content", "")
        if not args.get("title") or not content:
            return tool_error("write/append require 'title' and 'content'")
        content = self._auto_link(content, own_title=title)
        path = self._resolve_in_hermes_dir(f"{self._folder_name}/Notes/{title}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            if overwrite or not path.exists():
                tags = args.get("tags") or []
                body = self._frontmatter(tags) + content.rstrip() + "\n"
                _atomic_write(path, body)
                created = True
            else:
                existing = path.read_text(encoding="utf-8", errors="replace")
                _atomic_write(path, existing.rstrip() + "\n\n" + content.rstrip() + "\n")
                created = False
        self._index_note(path)
        return json.dumps({"path": self._rel(path), "status": "written" if created else "appended"})

    def _tool_delete(self, args: dict) -> str:
        rel = args.get("path", "").strip()
        if not rel:
            return tool_error("delete requires 'path'")
        path = self._resolve_in_hermes_dir(rel)
        if not path.exists() or not path.is_file():
            return tool_error(f"Note not found: {rel}")
        path.unlink()
        self._index_note(path)
        return json.dumps({"path": rel, "status": "deleted"})

    @staticmethod
    def _frontmatter(tags: List[str]) -> str:
        clean_tags = [re.sub(r"[^\w/-]", "", str(t)) for t in tags if str(t).strip()]
        lines = ["---", f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "source: iris"]
        if clean_tags:
            lines.append("tags: [" + ", ".join(clean_tags) + "]")
        lines.append("---")
        return "\n".join(lines) + "\n\n"

    # -- Auto-linking -----------------------------------------------------------

    def _auto_link(self, content: str, *, own_title: str = "") -> str:
        """Wikilink mentions of existing note titles so the graph densifies.

        Conservative: whole-word matches only, longest titles first, never
        inside code fences or existing links, capped at _AUTO_LINK_MAX links.
        """
        if not self._auto_link_enabled or not self._index:
            return content
        try:
            titles = self._index.titles()
        except Exception:
            return content
        candidates = sorted(
            (t for t in titles
             if len(t) >= _AUTO_LINK_MIN_TITLE_LEN and t != own_title),
            key=len, reverse=True,
        )
        # Split on code fences; only even segments are prose.
        segments = content.split("```")
        added = 0
        for title in candidates:
            if added >= _AUTO_LINK_MAX:
                break
            if f"[[{title}" in content:
                continue
            pattern = re.compile(
                r"(?<!\[)\b" + re.escape(title) + r"\b(?!\]|\|)", re.IGNORECASE
            )
            for i in range(0, len(segments), 2):
                m = pattern.search(segments[i])
                if not m:
                    continue
                matched = m.group(0)
                link = f"[[{title}]]" if matched == title else f"[[{title}|{matched}]]"
                segments[i] = segments[i][:m.start()] + link + segments[i][m.end():]
                content = "```".join(segments)
                added += 1
                break
        return content

    # -- Vault scanning (fallback when FTS5 is unavailable) -----------------------

    def _iter_vault_files(self, root: Optional[Path] = None, limit: int = _MAX_SCAN_FILES):
        """Yield markdown files under root (default: vault), bounded and safe."""
        base = root or self._vault
        if not base or not base.is_dir():
            return
        count = 0
        stack = [base]
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
                    if count >= limit:
                        return

    def _search_vault(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        """Search the vault: FTS5 index when available, bounded scan otherwise."""
        if self._index:
            self._index.refresh()
            hits = self._index.search(query, limit=limit)
            if hits or self._index.available:
                return hits
        return self._scan_search(query, limit=limit)

    def _scan_search(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        """Bounded keyword scan over the vault's markdown files."""
        words = [w for w in re.findall(r"\w{3,}", query.lower())][:12]
        if not words:
            return []
        deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
        scored: List[tuple] = []
        for path in self._iter_vault_files():
            if time.monotonic() > deadline:
                break
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lower = text.lower()
            name_lower = path.stem.lower()
            score = sum(lower.count(w) for w in words)
            score += sum(5 for w in words if w in name_lower)
            if score > 0:
                scored.append((score, path, text))
        scored.sort(key=lambda item: item[0], reverse=True)

        results = []
        for score, path, text in scored[:limit]:
            results.append({
                "path": self._rel(path),
                "score": score,
                "snippet": self._snippet(text, words),
            })
        return results

    @staticmethod
    def _snippet(text: str, words: List[str], max_chars: int = 400) -> str:
        """Return the most relevant few lines of a note for a word list."""
        best_line, best_idx = 0, 0
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            lower = line.lower()
            hits = sum(1 for w in words if w in lower)
            if hits > best_line:
                best_line, best_idx = hits, idx
        start = max(0, best_idx - 1)
        chunk = "\n".join(lines[start:start + 4]).strip()
        return chunk[:max_chars] if chunk else text[:max_chars].strip()

    # -- Mirroring built-in memory ---------------------------------------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._vault or not self._write_enabled:
            return
        if action not in ("add", "replace", "remove"):
            return
        try:
            self._regenerate_mirror(target)
        except Exception as e:
            logger.debug("Obsidian memory mirror failed: %s", e)

    def _regenerate_mirror(self, target: str) -> None:
        """Rebuild the vault mirror note from the built-in store's on-disk state.

        The hook fires after the built-in tool has saved, so MEMORY.md/USER.md
        are current. Regenerating (instead of appending) keeps the vault note
        exactly in sync through replace/remove, not just add.
        """
        from tools.memory_tool import ENTRY_DELIMITER, get_memory_dir

        source = get_memory_dir() / ("USER.md" if target == "user" else "MEMORY.md")
        entries: List[str] = []
        if source.exists():
            raw = source.read_text(encoding="utf-8", errors="replace")
            entries = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]

        note = "User Profile.md" if target == "user" else "Memory.md"
        title = "User Profile" if target == "user" else "Memory"
        path = self._hermes_dir / note
        if not entries and not path.exists():
            return
        lines = [
            f"# {title}",
            "",
            f"Mirror of Iris's built-in memory ({source.name}). Regenerated on every "
            "memory write — prune entries by asking Iris, not by editing this file.",
            "",
        ]
        for entry in entries:
            lines.append("- " + entry.replace("\n", "\n  "))
        if not entries:
            lines.append("_(empty)_")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            _atomic_write(path, "\n".join(lines) + "\n")
        self._index_note(path)

    # -- Session notes ------------------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        # Sessions are persisted once, at session end — per-turn writes would
        # thrash vault sync (Obsidian Sync, Syncthing, iCloud).
        pass

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._vault or not self._write_enabled:
            return
        exchanges = self._extract_exchanges(messages)
        if not exchanges:
            return
        summary = self._summarize_session(exchanges)
        session_link = ""
        if self._session_notes:
            try:
                session_link = self._write_session_note(exchanges, summary)
            except Exception as e:
                logger.debug("Obsidian session note failed: %s", e)
        if self._daily_notes:
            try:
                self._append_daily_note(exchanges, summary, session_link)
            except Exception as e:
                logger.debug("Obsidian daily note failed: %s", e)

    def _write_session_note(self, exchanges: List[tuple], summary: str) -> str:
        """Write the Sessions/ note; returns its vault-relative link target."""
        stamp = datetime.now()
        sid = re.sub(r"[^\w-]", "", (self._session_id or "session"))[:24]
        title = f"{stamp.strftime('%Y-%m-%d %H%M')} {sid}"
        path = self._hermes_dir / "Sessions" / f"{title}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "---",
            f"created: {stamp.strftime('%Y-%m-%d %H:%M')}",
            "source: iris",
            "tags: [iris/session]",
            "---",
            "",
            f"# Session {stamp.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"{len(exchanges)} exchange(s). See also [[Memory]] and [[User Profile]].",
            "",
        ]
        if summary:
            lines += ["## Summary", "", summary.strip(), "", "## Transcript", ""]
        max_exchanges = 10 if summary else 30
        for user_text, assistant_text in exchanges[:max_exchanges]:
            lines.append(f"## {self._first_line(user_text)}" if not summary
                         else f"### {self._first_line(user_text)}")
            lines.append(f"**User:** {self._clip(user_text)}")
            lines.append("")
            lines.append(f"**Iris:** {self._clip(assistant_text)}")
            lines.append("")
        with self._write_lock:
            _atomic_write(path, "\n".join(lines))
        self._index_note(path)
        return self._rel(path)[:-3]  # strip .md for the wikilink

    def _append_daily_note(
        self, exchanges: List[tuple], summary: str, session_link: str
    ) -> None:
        """Append a marked session section to the day's daily note.

        This is the single opt-in exception to the write scope: daily notes
        live wherever the user keeps them (``daily_notes_folder``), and the
        section is clearly attributed to Iris.
        """
        stamp = datetime.now()
        name = stamp.strftime(self._daily_note_format) + ".md"
        rel = f"{self._daily_notes_folder}/{name}" if self._daily_notes_folder else name
        path = self._resolve_in_vault(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = summary.strip() if summary else self._first_line(exchanges[0][0], 200)
        section = [f"## Iris — {stamp.strftime('%H:%M')} session", "", body]
        if session_link:
            section += ["", f"Full note: [[{session_link}]]"]
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8", errors="replace").rstrip()
        else:
            existing = f"# {stamp.strftime(self._daily_note_format)}"
        with self._write_lock:
            _atomic_write(path, existing + "\n\n" + "\n".join(section) + "\n")

    def _summarize_session(self, exchanges: List[tuple]) -> str:
        """LLM summary via the auxiliary client; empty string on any failure."""
        if self._session_summary != "auto":
            return ""
        transcript_parts = []
        for user_text, assistant_text in exchanges[:20]:
            transcript_parts.append(f"User: {self._clip(user_text, 800)}")
            transcript_parts.append(f"Iris: {self._clip(assistant_text, 800)}")
        transcript = "\n\n".join(transcript_parts)[:8000]
        try:
            from agent.auxiliary_client import call_llm

            response = call_llm(
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                max_tokens=400,
                temperature=0.2,
                timeout=45,
            )
            text = (response.choices[0].message.content or "").strip()
            # A summary longer than the transcript defeats the purpose.
            if text and len(text) < len(transcript):
                return text
        except Exception as e:
            logger.debug("Session summary LLM call failed, using digest: %s", e)
        return ""

    @staticmethod
    def _extract_exchanges(messages: List[Dict[str, Any]]) -> List[tuple]:
        exchanges = []
        pending_user = None
        for msg in messages or []:
            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                pending_user = content
            elif role == "assistant" and pending_user:
                exchanges.append((pending_user, content))
                pending_user = None
        return exchanges

    @staticmethod
    def _first_line(text: str, max_chars: int = 80) -> str:
        line = (text.strip().splitlines() or [""])[0]
        return line[:max_chars] + ("…" if len(line) > max_chars else "")

    @staticmethod
    def _clip(text: str, max_chars: int = 600) -> str:
        text = text.strip()
        return text[:max_chars] + ("…" if len(text) > max_chars else "")


def register(ctx) -> None:
    """Register the Obsidian memory provider with the plugin system."""
    ctx.register_memory_provider(ObsidianMemoryProvider())
