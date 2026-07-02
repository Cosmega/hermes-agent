"""Obsidian vault memory plugin using the MemoryProvider interface.

Persists agent memory as plain Markdown notes inside a local Obsidian
vault. Everything is local-first: no server, no API key, no network.
The vault stays a normal Obsidian vault — notes are readable, editable,
linkable, and sync however the user already syncs their vault.

Vault layout (all under the configured subfolder, default "Hermes"):
  <folder>/Memory.md           — mirrored built-in MEMORY.md entries
  <folder>/User Profile.md     — mirrored built-in USER.md entries
  <folder>/Notes/<Title>.md    — notes the agent creates via the tool
  <folder>/Sessions/<date>.md  — end-of-session conversation logs

Write scope: the agent can READ and SEARCH the whole vault, but can only
WRITE and DELETE inside the configured Hermes subfolder. User notes are
never modified.

Config in $HERMES_HOME/config.yaml:
  plugins:
    obsidian-memory:
      vault_path: ~/Documents/MyVault   # required — path to the vault
      folder: Hermes                    # subfolder Hermes writes into
      session_notes: true               # write a note at session end
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from hermes_cli.config import cfg_get
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Bounds for vault scans so prefetch/search stay fast on large vaults.
_MAX_SCAN_FILES = 500
_MAX_FILE_BYTES = 262_144  # 256 KB — skip anything bigger
_SCAN_BUDGET_SECONDS = 1.5
_SKIP_DIRS = {".obsidian", ".trash", ".git", ".sync-conflicts"}

# Characters Obsidian forbids in note titles, plus path separators.
_TITLE_SANITIZE_RE = re.compile(r'[\\/:*?"<>|#^\[\]]')


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


class ObsidianMemoryProvider(MemoryProvider):
    """Markdown-note memory backed by a local Obsidian vault."""

    def __init__(self, config: dict | None = None):
        self._config = config if config is not None else _load_plugin_config()
        self._vault: Optional[Path] = None
        self._folder_name = str(self._config.get("folder", "Hermes")).strip() or "Hermes"
        self._session_notes = _as_bool(self._config.get("session_notes", True))
        self._session_id = ""
        self._write_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._prefetch_query = ""
        self._prefetch_result = ""

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
                "description": "Subfolder inside the vault where Hermes writes its notes",
                "default": "Hermes",
            },
            {
                "key": "session_notes",
                "description": "Write a conversation log note at the end of each session",
                "default": "true",
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

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id

    def shutdown(self) -> None:
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
        """Resolve a path and require it to be inside the Hermes folder."""
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
        # No cached background result — do a bounded synchronous scan.
        try:
            return self._format_search_context(query)
        except Exception as e:
            logger.debug("Obsidian prefetch scan failed: %s", e)
            return ""

    def _format_search_context(self, query: str) -> str:
        hits = self._search_vault(query, limit=3)
        if not hits:
            return ""
        lines = ["## Obsidian Memory (relevant notes)"]
        for hit in hits:
            lines.append(f"### [[{hit['path']}]]")
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
        path = self._resolve_in_hermes_dir(f"{self._folder_name}/Notes/{title}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            if overwrite or not path.exists():
                tags = args.get("tags") or []
                body = self._frontmatter(tags) + content.rstrip() + "\n"
                path.write_text(body, encoding="utf-8")
                created = True
            else:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n" + content.rstrip() + "\n")
                created = False
        return json.dumps({"path": self._rel(path), "status": "written" if created else "appended"})

    def _tool_delete(self, args: dict) -> str:
        rel = args.get("path", "").strip()
        if not rel:
            return tool_error("delete requires 'path'")
        path = self._resolve_in_hermes_dir(rel)
        if not path.exists() or not path.is_file():
            return tool_error(f"Note not found: {rel}")
        path.unlink()
        return json.dumps({"path": rel, "status": "deleted"})

    @staticmethod
    def _frontmatter(tags: List[str]) -> str:
        clean_tags = [re.sub(r"[^\w/-]", "", str(t)) for t in tags if str(t).strip()]
        lines = ["---", f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "source: hermes"]
        if clean_tags:
            lines.append("tags: [" + ", ".join(clean_tags) + "]")
        lines.append("---")
        return "\n".join(lines) + "\n\n"

    # -- Vault scanning ------------------------------------------------------------

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
        """Bounded keyword search over the vault's markdown files."""
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
        if not self._vault or not getattr(self, "_write_enabled", True):
            return
        if action != "add" or not content:
            return
        note = "User Profile.md" if target == "user" else "Memory.md"
        try:
            path = self._hermes_dir / note
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d")
            with self._write_lock:
                new_file = not path.exists()
                with open(path, "a", encoding="utf-8") as f:
                    if new_file:
                        title = "User Profile" if target == "user" else "Memory"
                        f.write(f"# {title}\n\nMirrored from Hermes built-in memory.\n\n")
                    f.write(f"- {stamp} — {content.strip()}\n")
        except Exception as e:
            logger.debug("Obsidian memory mirror failed: %s", e)

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
        if not self._vault or not self._session_notes:
            return
        if not getattr(self, "_write_enabled", True):
            return
        exchanges = self._extract_exchanges(messages)
        if not exchanges:
            return
        try:
            stamp = datetime.now()
            sid = re.sub(r"[^\w-]", "", (self._session_id or "session"))[:24]
            title = f"{stamp.strftime('%Y-%m-%d %H%M')} {sid}"
            path = self._hermes_dir / "Sessions" / f"{title}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "---",
                f"created: {stamp.strftime('%Y-%m-%d %H:%M')}",
                "source: hermes",
                "tags: [hermes/session]",
                "---",
                "",
                f"# Session {stamp.strftime('%Y-%m-%d %H:%M')}",
                "",
                f"{len(exchanges)} exchange(s). See also [[Memory]] and [[User Profile]].",
                "",
            ]
            for user_text, assistant_text in exchanges[:30]:
                lines.append(f"## {self._first_line(user_text)}")
                lines.append(f"**User:** {self._clip(user_text)}")
                lines.append("")
                lines.append(f"**Hermes:** {self._clip(assistant_text)}")
                lines.append("")
            with self._write_lock:
                path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.debug("Obsidian session note failed: %s", e)

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
