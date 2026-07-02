# Obsidian Memory Provider

Persists Hermes Agent memory as plain Markdown notes inside a local
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
    folder: Hermes                    # subfolder Hermes writes into
    session_notes: true               # write a log note at session end
```

## Vault layout

Everything Hermes writes lives under the configured subfolder (default `Hermes/`):

```
MyVault/
└── Hermes/
    ├── Memory.md            # mirrored built-in MEMORY.md entries
    ├── User Profile.md      # mirrored built-in USER.md entries
    ├── Notes/               # notes the agent creates via the obsidian tool
    │   └── Project Odysseus.md
    └── Sessions/            # end-of-session conversation logs
        └── 2026-07-02 1430 a1b2c3.md
```

## Write scope (safety)

- **Read/search:** the whole vault (minus `.obsidian/`, `.trash/`, dotfiles).
- **Write/append/delete:** only inside the `Hermes/` subfolder. The agent can
  never modify or delete your own notes.
- Path traversal (`../`) out of the vault is rejected.

## Tool

One tool, `obsidian`, with six actions:

| Action | Scope | Description |
|--------|-------|-------------|
| `search` | vault | Bounded keyword search (500 files / 1.5 s budget) |
| `read` | vault | Read a note by vault-relative path |
| `list` | vault | List notes in the vault or a subfolder |
| `write` | Hermes folder | Create/overwrite a note (frontmatter + tags) |
| `append` | Hermes folder | Append to a note (creates if missing) |
| `delete` | Hermes folder | Delete an agent-created note |

## Behavior

- **Prefetch:** before each turn, a background keyword search injects the top
  3 relevant note snippets as context.
- **Mirroring:** every `add` from the built-in memory tool is appended (with a
  date stamp) to `Memory.md` or `User Profile.md`. `replace`/`remove` are not
  mirrored — the vault is an append-only journal; prune it in Obsidian.
- **Session notes:** at real session boundaries (CLI exit, `/reset`, gateway
  expiry) a digest note with truncated exchanges is written to `Sessions/`.
  Disable with `session_notes: false`.
- **Cron/subagents:** non-primary agent contexts never write to the vault.

## Backup

The vault is intentionally **not** included in `hermes backup` — it's your
document store, already covered by your own vault sync/backup strategy.
