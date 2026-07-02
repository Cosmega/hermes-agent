# Iris — Hermes Agent, fully self-hosted ☤

**Iris** is a preset for running Hermes Agent as a completely
self-hosted personal AI agent:

| Pillar | How |
|--------|-----|
| **Unlimited credits** | Inference runs on your own hardware (Ollama, LM Studio, vLLM, llama.cpp). No API keys, no per-token billing, no rate limits but your GPU. |
| **Secure conversations** | Everything stays on your machine — model, session history, memory, even voice transcription. Gateway hardened with user allowlists, DM pairing, and dangerous-command approval. Signal supported for end-to-end encrypted messaging. |
| **Memory in Obsidian** | The `obsidian` memory provider persists the agent's memory as plain Markdown notes in your vault — FTS5-indexed, LLM-summarized session notes, daily-note integration, auto-wikilinked into your knowledge graph. |

No cloud service sees your prompts, your history, or your notes.

---

## Quick start (one command)

With Hermes installed and an Obsidian vault on disk:

```bash
scripts/setup-iris.sh --vault ~/Documents/MyVault
hermes    # fly.
```

The script pulls a local model through Ollama, points Hermes at it, enables
manual approval for dangerous commands, activates Obsidian memory, and writes
the Iris persona to `SOUL.md` (never overwriting an existing one). Re-run it
any time — it's idempotent. `--dry-run` shows what it would do; `--skip-model`
keeps your current provider. The sections below explain each piece.

---

## 1. Self-hosted inference (unlimited credits)

Pick one local server. Hermes speaks to any OpenAI-compatible endpoint.

### Which model fits your hardware?

Rough guide for quantized (Q4) GGUF models — the common case on Ollama:

| VRAM / unified memory | Models that run comfortably | Notes |
|---|---|---|
| CPU only / ≤ 4 GB | `qwen3:1.7b`, `llama3.2:3b` | Fine for chat + memory; weak at tool-heavy work |
| 8 GB | `hermes3:8b`, `qwen3:8b`, `llama3.1:8b` | The sweet spot for Iris on a gaming GPU |
| 12–16 GB | `qwen3:14b`, `mistral-small3.1` | Noticeably better tool-calling reliability |
| 24 GB | `hermes3:70b` (heavily quantized), `qwen3:32b`, `gemma3:27b` | Strong daily driver |
| 48 GB+ / Mac Studio | `hermes3:70b`, `llama3.3:70b`, `deepseek-r1:70b` | Cloud-model territory, fully local |

Tool calling is what an agent stresses most — when in doubt, prefer the
larger model at a lower quant over the smaller model at full precision.

### Ollama (easiest)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull hermes3          # or any model from the table above
```

`~/.hermes/config.yaml`:

```yaml
model:
  provider: "ollama"                       # alias of "custom"
  base_url: "http://127.0.0.1:11434/v1"
  default: "hermes3"
```

### LM Studio (first-class provider)

```yaml
model:
  provider: "lmstudio"                     # defaults to http://127.0.0.1:1234/v1
  default: "your-loaded-model"
```

### vLLM / llama.cpp (server-grade)

```yaml
model:
  provider: "vllm"                         # or "llamacpp" — both alias "custom"
  base_url: "http://127.0.0.1:8000/v1"
  default: "NousResearch/Hermes-3-Llama-3.1-8B"
```

Tips for local endpoints:

```yaml
providers:
  ollama-local:
    request_timeout_seconds: 300   # allow cold-start model loads
model:
  context_length: 32768            # set only if your server's num_ctx differs
                                   # from what /v1/models reports
```

Switch models any time with `hermes model` or `/model` in a conversation.

## 2. Secure conversations

Local inference already keeps prompts and completions on your machine.
Session history, MEMORY.md, USER.md, and skills all live under `~/.hermes/`
— never in a cloud. Harden the rest:

```yaml
# ~/.hermes/config.yaml
approvals:
  mode: "manual"       # human-in-the-loop for dangerous commands (manual | smart | off)
```

For messaging access, run `hermes gateway setup` and:

- **Allowlist users** — only your own account can talk to the bot.
- **DM pairing** — unknown senders must present a pairing code.
- **Signal** — use the Signal platform for end-to-end encrypted transport.
- **Container isolation** — run the agent's shell in Docker
  (`terminal.backend: docker`) so tool calls can't touch the host.

### Voice, fully local

Voice memos from Telegram/Signal are transcribed with the built-in **local**
STT provider (faster-whisper) — no audio ever leaves the machine:

```yaml
stt:
  enabled: true
  provider: "local"
  local:
    model: "base"      # tiny | base | small | medium | large-v3 | turbo
```

### Encryption at rest

Iris keeps everything under `~/.hermes/` and your vault — in plaintext, like
any local app. If the machine is shared, portable, or a VPS, encrypt the
storage layer underneath rather than per-file:

- **Laptop:** full-disk encryption (LUKS on Linux, FileVault on macOS) covers
  both `~/.hermes/` and the vault with zero config.
- **VPS / always-on box:** put `~/.hermes/` (and the vault) on an encrypted
  directory, e.g. [gocryptfs](https://nuetzlich.net/gocryptfs/):
  `gocryptfs ~/.hermes.enc ~/.hermes` — mounted at boot, opaque at rest.
- `hermes backup` archives can be piped through `age` before leaving the
  machine: `hermes backup && age -p backup.tar.gz > backup.tar.gz.age`.

Full reference: [Security guide](https://hermes-agent.nousresearch.com/docs/user-guide/security).

## 3. Memory in Obsidian

Activate the bundled [`obsidian` memory provider](plugins/memory/obsidian/README.md):

```bash
hermes memory setup      # select "obsidian", point it at your vault
```

Or manually:

```yaml
# ~/.hermes/config.yaml
memory:
  provider: obsidian

plugins:
  obsidian-memory:
    vault_path: ~/Documents/MyVault
    folder: Iris                # the agent's subfolder in your vault
    session_notes: true
    session_summary: auto       # LLM-written session summaries (digest fallback)
    daily_notes: false          # opt-in: session recap in your daily note
    auto_link: true             # auto-[[wikilink]] known note titles
```

What you get inside the vault:

```
MyVault/
└── Iris/
    ├── Memory.md          # live mirror of everything Iris remembers
    ├── User Profile.md    # what it learns about you (kept exactly in sync)
    ├── Notes/             # notes it writes (frontmatter, #tags, [[wikilinks]])
    └── Sessions/          # end-of-session notes: LLM summary + transcript
```

- Iris reads and searches your **whole vault** (backed by an incremental
  FTS5 index kept outside the vault) but writes **only inside `Iris/`** —
  your notes are never touched. The one opt-in exception: `daily_notes: true`
  appends a marked `## Iris — HH:MM session` recap to your daily note.
- The `Memory.md` / `User Profile.md` mirrors are regenerated on every
  memory write — adds, edits, and removals all stay in sync.
- Notes Iris writes auto-link to existing notes in your vault, so the graph
  view fills in by itself.

## 4. The Iris persona (optional)

`SOUL.md` is the agent's identity — slot #1 of the system prompt. The setup
script installs it for you; to do it by hand, put this in `~/.hermes/SOUL.md`:

```markdown
# Iris

You are Iris — messenger of the gods, a self-hosted personal agent running
entirely on your user's own hardware. Nothing you read or write leaves this
machine.

Voice: swift, luminous, direct. You carry messages faithfully — you say what
you know, what you don't, and what you'd try next. No flattery, no filler.

Memory: your long-term memory is the user's Obsidian vault. Search it before
answering questions about the user, their projects, or past decisions.
Persist durable knowledge as linked notes so the knowledge graph deepens —
like a rainbow, every note you leave should connect two points.
```

## 5. Iris Web — a minimal localhost UI (optional)

If you'd rather talk to Iris in a browser than a terminal, a deliberately
simple web UI ships in [`apps/iris-web/`](apps/iris-web/README.md) — one
static page + one stdlib Python server, no build step, no npm:

```bash
# ~/.hermes/.env
API_SERVER_ENABLED=true
API_SERVER_KEY=pick-something-random

hermes gateway                      # terminal 1 — agent + API server
python3 apps/iris-web/serve.py     # terminal 2 → http://127.0.0.1:8643
```

Streaming replies, live tool-activity lines, markdown rendering,
conversation kept in your browser. The API key stays server-side; both
servers bind to 127.0.0.1 only. (The full-featured dashboard remains
available via `hermes dashboard`.)

## Manual setup (the script, unrolled)

```bash
# 1. Install Hermes
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# 2. Local model
ollama pull hermes3
hermes config set model.provider ollama
hermes config set model.base_url http://127.0.0.1:11434/v1
hermes config set model.default hermes3

# 3. Obsidian memory
hermes memory setup          # select "obsidian"

# 4. Persona + hardening
$EDITOR ~/.hermes/SOUL.md    # paste the Iris persona
hermes config set approvals.mode manual

hermes                       # fly.
```
