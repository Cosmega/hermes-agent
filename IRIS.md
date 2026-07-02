# Iris — Hermes Agent, fully self-hosted ☤

**Iris** is a preset for running Hermes Agent as a completely
self-hosted personal AI agent:

| Pillar | How |
|--------|-----|
| **Unlimited credits** | Inference runs on your own hardware (Ollama, LM Studio, vLLM, llama.cpp). No API keys, no per-token billing, no rate limits but your GPU. |
| **Secure conversations** | Everything stays on your machine — model, session history, memory. Gateway hardened with user allowlists, DM pairing, and dangerous-command approval. Signal supported for end-to-end encrypted messaging. |
| **Memory in Obsidian** | The `obsidian` memory provider persists the agent's memory as plain Markdown notes in your vault — readable, editable, wikilinked into your knowledge graph. |

No cloud service sees your prompts, your history, or your notes.

---

## 1. Self-hosted inference (unlimited credits)

Pick one local server. Hermes speaks to any OpenAI-compatible endpoint.

### Ollama (easiest)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull hermes3          # or qwen3, llama3.3, deepseek-r1, …
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
    folder: Hermes
    session_notes: true
```

What you get inside the vault:

```
MyVault/
└── Hermes/
    ├── Memory.md          # everything the agent chooses to remember
    ├── User Profile.md    # what it learns about you
    ├── Notes/             # notes it writes (frontmatter, #tags, [[wikilinks]])
    └── Sessions/          # end-of-session conversation digests
```

The agent reads and searches your **whole vault** (so it can answer from your
own notes) but writes **only inside `Hermes/`** — your notes are never touched.

## 4. The Iris persona (optional)

`SOUL.md` is the agent's identity — slot #1 of the system prompt. To name
your instance Iris, put this in `~/.hermes/SOUL.md`:

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

## Quick start (all four pieces)

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
