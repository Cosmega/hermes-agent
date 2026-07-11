# Obsidian Memory Provider

Persists the agent's memory as plain Markdown notes inside a local
[Obsidian](https://obsidian.md) vault. Local-first: no server, no API key,
no network calls, no per-request cost. Your memory is your vault — readable,
editable, linkable, and synced however you already sync it (Obsidian Sync,
Syncthing, iCloud, git).

## Setup

```bash
hermes memory setup          # select "obsidian", point it at your vault
# Or manually:
hermes config set memory.provider obsidian
```

Config lives in `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: obsidian

plugins:
  obsidian-memory:
    vault_path: ~/Documents/MyVault   # required — the folder Obsidian opens
    folder: Iris                      # subfolder the agent writes into
    session_notes: true               # write a note at session end
    session_summary: auto             # auto = LLM summary w/ digest fallback, off = digest only
    daily_notes: false                # opt-in: append a session section to your daily note
    daily_notes_folder: ""            # vault-relative daily notes folder ("" = vault root)
    daily_note_format: "%Y-%m-%d"     # daily note filename date format
    auto_link: true                   # wikilink existing note titles in written notes
```

## Vault layout

Everything the agent writes lives under the configured subfolder (default `Iris/`):

```
MyVault/
└── Iris/
    ├── Memory.md            # live mirror of the built-in MEMORY.md
    ├── User Profile.md      # live mirror of the built-in USER.md
    ├── Notes/               # notes the agent creates via the obsidian tool
    │   └── Project Iris.md
    └── Sessions/            # end-of-session notes (LLM summary + transcript)
        └── 2026-07-02 1430 a1b2c3.md
```

## Write scope (safety)

- **Read/search:** the whole vault (minus `.obsidian/`, `.trash/`, dotfiles).
- **Write/append/delete:** only inside the `Iris/` subfolder. The agent can
  never modify or delete your own notes.
- **One opt-in exception:** with `daily_notes: true`, a clearly-marked
  `## Iris — HH:MM session` section is appended to the day's daily note.
- Path traversal (`../`) out of the vault is rejected.
- All writes are atomic (temp file + rename) so vault sync (Obsidian Sync,
  Syncthing, iCloud) never sees a half-written note.

## Search: FTS5 index

Search and prefetch are backed by an incremental SQLite FTS5 index stored at
`$HERMES_HOME/obsidian_index.db` — **outside** the vault, so it never shows
up in Obsidian or your sync. A cheap mtime walk re-reads only changed files
(throttled to one pass per 20 s); the agent's own writes are indexed
immediately. Vaults of tens of thousands of notes stay fast. If your SQLite
lacks FTS5, the provider transparently falls back to a bounded text scan.

## Tool

One tool, `obsidian`, with six actions:

| Action | Scope | Description |
|--------|-------|-------------|
| `search` | vault | Full-text search (FTS5 index, scan fallback) |
| `read` | vault | Read a note by vault-relative path |
| `list` | vault | List notes in the vault or a subfolder |
| `write` | Iris folder | Create/overwrite a note (frontmatter + tags) |
| `append` | Iris folder | Append to a note (creates if missing) |
| `delete` | Iris folder | Delete an agent-created note |

## Behavior

- **Prefetch:** before each turn, a background search injects the top 3
  relevant note snippets as context.
- **Mirroring:** on every built-in memory write (`add`, `replace`, **and**
  `remove`), `Iris/Memory.md` / `Iris/User Profile.md` are regenerated from
  the built-in store's on-disk state — the vault mirror never drifts or
  accumulates stale entries.
- **Session notes:** at real session boundaries (CLI exit, `/reset`, gateway
  expiry) a note is written to `Sessions/`. With `session_summary: auto`
  (default), the auxiliary LLM produces a 3–6 bullet summary (decisions,
  facts, follow-ups); if the call fails the note falls back to a plain
  transcript digest. Disable notes entirely with `session_notes: false`.
- **Daily notes:** with `daily_notes: true`, the session summary is also
  appended to your Obsidian daily note, wikilinked to the full session note.
- **Auto-linking:** titles of existing vault notes mentioned in agent-written
  content become `[[wikilinks]]` automatically (whole-word matches, longest
  first, never inside code fences or existing links, max 8 per note, titles
  ≥ 4 chars). Disable with `auto_link: false`.
- **Cron/subagents:** non-primary agent contexts never write to the vault.

## Iris Inbox — the vault as a command surface

Write a note anywhere in your vault, tag it `#iris/task`, and Iris runs it
and appends the answer to the note itself. Your sync (Obsidian Sync,
Syncthing, iCloud) is the transport — no chat app, no open port.

```markdown
---
tags: [iris/task]
---
Compare the three frameworks in [[SSG Research]] and recommend one.
```

Start the watcher:

```bash
iris obsidian inbox              # watch loop (default: every 30 s)
iris obsidian inbox --once       # single pass — cron/systemd friendly
iris obsidian status             # config + recent activity
```

Iris drives a state machine in the note's frontmatter
(`iris-status: running → done | failed | blocked`) and appends its answer
under `## ✦ Iris — <date>` — your own text is never modified. Failed notes
carry a retry hint (remove `iris-status` to re-run).

**Recurring tasks:** add `iris-cron: "0 7 * * *"` to the frontmatter — the
note re-runs on schedule and each run appends a new dated section. Your
daily briefing, defined in a note.

**Safety:** note text is untrusted input that becomes a prompt, so every
candidate is screened with the shared threat-pattern library (a hit marks
the note `blocked` instead of executing); executions are rate-limited
(`inbox_max_per_hour`, default 6); `inbox_folder` can restrict scanning to
one subfolder; and each task runs as a normal one-shot agent session, so
your `approvals` policy applies to dangerous commands.

| Config key | Default | Description |
|-----------|---------|-------------|
| `inbox_folder` | `""` | Vault-relative scan scope (`""` = whole vault) |
| `inbox_max_per_hour` | `6` | Max task executions per hour |
| `inbox_task_timeout` | `900` | Seconds per task |

## Backup

The vault is intentionally **not** included in `hermes backup` — it's your
document store, already covered by your own vault sync/backup strategy. The
FTS index is disposable (it rebuilds itself from the vault).
