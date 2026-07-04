# Iris Web

A deliberately minimal localhost chat UI for your agent. One static HTML
page + one stdlib Python server. No build step, no npm, no React, no
dependencies — `python3 serve.py` and you're chatting.

![architecture](#) `browser → serve.py (127.0.0.1:8643) → Hermes API server (127.0.0.1:8642) → agent`

For the full-featured dashboard, use `hermes dashboard` (the Vite/React app
under `web/`). Iris Web is the opposite trade-off: something you can read
in one sitting and hack on in one edit.

## Run it

1. Enable the API server in `~/.hermes/.env`:

   ```bash
   API_SERVER_ENABLED=true
   API_SERVER_KEY=pick-something-random
   ```

2. Start the gateway and the UI:

   ```bash
   hermes gateway                       # terminal 1
   python3 apps/iris-web/serve.py      # terminal 2 → http://127.0.0.1:8643
   ```

Options: `--port 8643`, `--host 127.0.0.1`, `--api http://127.0.0.1:8642`.

## What it does

- **Streaming chat** with the full agent (tools, memory, skills) via the
  OpenAI-compatible `/v1/chat/completions` endpoint — tool activity shows
  up live as `hermes.tool.progress` lines under the reply.
- **Markdown-lite rendering** (code blocks, inline code, bold, lists,
  links, headings) with HTML escaping first — no external renderer.
- **Conversation persistence** in the browser's localStorage; "New chat"
  clears it. The API is stateless — the full history is sent each turn.
- **Key stays server-side**: the page talks only to `serve.py`, which
  injects `Authorization: Bearer $API_SERVER_KEY` when proxying. The key
  is read from the environment or `$HERMES_HOME/.env` and never reaches
  the browser.
- **Status dot** polls `/api/health` and tells you exactly what's wrong
  (`gateway down`, `API_SERVER_KEY missing`).

## Security notes

- Binds to `127.0.0.1` by default — keep it that way unless you know what
  you're doing; there is no auth on the UI itself.
- `serve.py` serves exactly one file (`index.html`) and exposes exactly
  two API routes (`/api/health`, `/api/chat`). Everything else is 404.
- Stop button aborts the browser-side stream; the agent turn finishes
  server-side (matching the API server's stateless contract).
