# Iris ✦

```text
██╗██████╗ ██╗███████╗
██║██╔══██╗██║██╔════╝
██║██████╔╝██║███████╗
██║██╔══██╗██║╚════██║
██║██║  ██║██║███████║
╚═╝╚═╝  ╚═╝╚═╝╚══════╝
        the rainbow messenger  ✦
```

**Your self-hosted AI agent.** Iris runs entirely on your own hardware:
local inference (no API keys, no per-token billing), memory that lives as
Markdown notes in your Obsidian vault, and conversations that never leave
your machine.

| Pillar | How |
|--------|-----|
| **Unlimited credits** | Inference on your own GPU via Ollama, LM Studio, vLLM, or llama.cpp. The only rate limit is your hardware. |
| **Secure conversations** | Model, history, memory, and voice transcription all stay local. Hardened messaging gateway (allowlists, DM pairing, command approval), Signal for end-to-end encryption. |
| **Memory in Obsidian** | Everything Iris remembers is a readable, editable note in your vault — FTS5-indexed, session summaries, daily-note integration, auto-wikilinked into your graph. |

This repository is the **whole project**: engine, plugins, web UI, docs,
installers. Nothing depends on any other repository.

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/Cosmega/iris-agent/main/scripts/install-iris.sh | bash
```

That clones this repo to `~/iris`, sets up the Python environment, and puts
the `iris` command on your PATH. Then:

```bash
~/iris/scripts/setup-iris.sh --vault ~/Documents/MyVault   # local model + memory + persona
iris                                                       # fly.
```

Full guide — hardware sizing, security hardening, Obsidian memory, the
persona, updating: **[IRIS.md](IRIS.md)**.

## Talk to Iris

- **Terminal:** `iris` — full TUI with streaming, slash commands, sessions.
- **Browser:** [`apps/iris-web/`](apps/iris-web/README.md) — a minimal
  localhost chat page (`iris gateway` + `python3 apps/iris-web/serve.py`
  → http://127.0.0.1:8643).
- **Messaging:** `iris gateway setup` — Telegram, Discord, Slack, WhatsApp,
  Signal, with allowlists and pairing.

## What's inside

- **Local-first memory** — the [`obsidian` memory provider](plugins/memory/obsidian/README.md)
  reads and searches your whole vault, writes only in its own `Iris/`
  folder, mirrors everything it remembers as plain Markdown, and summarizes
  each session into your notes.
- **Iris Inbox** — tag any note `#iris/task` and Iris runs it and appends
  the answer to the note itself (`iris obsidian inbox`). Your vault sync is
  the transport: drop tasks from your phone, read results over coffee.
  `iris-cron:` frontmatter makes a note a recurring job.
- **A closed learning loop** — Iris creates skills from experience,
  improves them during use, and searches its own past conversations.
- **Scheduled automations** — built-in cron with delivery to any platform.
- **40+ tools** — terminal, files, browser, code execution, subagents.
- **Runs anywhere** — a $5 VPS, a gaming PC, a Mac; Docker/SSH/serverless
  terminal backends included.

## Docs

Everything is in-repo under [`website/docs/`](website/docs/):
[configuration](website/docs/user-guide/configuration.md) ·
[security](website/docs/user-guide/security.md) ·
[memory](website/docs/user-guide/features/memory.md) ·
[memory providers](website/docs/user-guide/features/memory-providers.md) ·
[messaging](website/docs/user-guide/messaging) ·
[skills](website/docs/user-guide/features/skills.md) ·
[cron](website/docs/user-guide/features/cron.md)

## Updating

```bash
cd ~/iris && git pull
```

`iris update` does the same (it pulls from this repository's origin — never
from anywhere else). Re-run the installer only if dependencies changed.

## License

MIT — see [LICENSE](LICENSE). Iris is built on the open-source
hermes-agent engine (MIT, Nous Research), fully vendored in this repository.
