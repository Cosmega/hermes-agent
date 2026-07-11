"""CLI commands for the Obsidian memory plugin.

Registered as ``iris obsidian ...`` while obsidian is the active memory
provider (the plugin CLI system only loads the active provider's cli.py).

Commands:
    iris obsidian inbox            # watch the vault for #iris/task notes
    iris obsidian inbox --once     # single scan-and-process pass (cron-friendly)
    iris obsidian status           # show inbox config and recent activity
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _plugin_config() -> dict:
    from hermes_cli.config import cfg_get, load_config

    return cfg_get(load_config() or {}, "plugins", "obsidian-memory", default={}) or {}


def _build_watcher():
    from hermes_constants import get_hermes_home

    from .inbox import InboxConfig, InboxWatcher

    cfg = _plugin_config()
    if not cfg.get("vault_path"):
        print("✗ obsidian memory is not configured — run 'iris memory setup' first.")
        return None
    inbox_cfg = InboxConfig.from_plugin_config(cfg, hermes_home=str(get_hermes_home()))
    if not inbox_cfg.vault_path.is_dir():
        print(f"✗ vault not found: {inbox_cfg.vault_path}")
        return None
    return InboxWatcher(inbox_cfg)


def _cmd_inbox(args) -> None:
    watcher = _build_watcher()
    if watcher is None:
        sys.exit(1)
    scope = watcher.config.folder or "(whole vault)"
    print(f"✦ Iris Inbox — vault: {watcher.config.vault_path}  scope: {scope}")
    print(f"  tag: #iris/task · limit: {watcher.config.max_per_hour}/h · "
          f"timeout: {watcher.config.task_timeout}s")
    if getattr(args, "once", False):
        res = watcher.run_once()
        print(f"  scanned: {res.scanned} runnable · done: {len(res.ran)} · "
              f"failed: {len(res.failed)} · blocked: {len(res.blocked)} · "
              f"deferred: {res.rate_limited}")
        for rel in res.ran:
            print(f"  ✦ done: {rel}")
        for rel in res.failed:
            print(f"  ✗ failed: {rel}")
        for rel in res.blocked:
            print(f"  ⛔ blocked: {rel}")
        sys.exit(0 if not res.failed else 1)
    interval = max(5.0, float(getattr(args, "interval", 30.0)))
    try:
        watcher.run_forever(interval=interval)
    except KeyboardInterrupt:
        print("\n✦ inbox stopped")


def _cmd_status(args) -> None:
    from hermes_constants import get_hermes_home

    cfg = _plugin_config()
    print("✦ Obsidian memory")
    print(f"  vault:  {cfg.get('vault_path', '(not configured)')}")
    print(f"  folder: {cfg.get('folder', 'Iris')}")
    print(f"  inbox scope: {cfg.get('inbox_folder', '') or '(whole vault)'}")
    print(f"  inbox limit: {cfg.get('inbox_max_per_hour', 6)}/h")
    state = Path(get_hermes_home()) / "obsidian_inbox_state.json"
    if state.exists():
        try:
            runs = json.loads(state.read_text(encoding="utf-8")).get("runs", [])
            recent = [t for t in runs if t > time.time() - 3600]
            print(f"  inbox runs: {len(recent)} in the last hour, {len(runs)} recorded")
        except Exception:
            pass


def register_cli(subparser) -> None:
    """Build the ``iris obsidian`` argparse subcommand tree."""
    subs = subparser.add_subparsers(dest="obsidian_command")

    inbox = subs.add_parser(
        "inbox",
        help="Watch the vault for #iris/task notes and run them",
    )
    inbox.add_argument("--once", action="store_true",
                       help="Single scan-and-process pass, then exit (cron-friendly)")
    inbox.add_argument("--interval", type=float, default=30.0,
                       help="Seconds between scans in watch mode (default 30)")

    subs.add_parser("status", help="Show obsidian memory + inbox status")


def obsidian_command(args) -> None:
    """Route ``iris obsidian`` subcommands."""
    sub = getattr(args, "obsidian_command", None)
    if sub == "inbox":
        _cmd_inbox(args)
    elif sub == "status":
        _cmd_status(args)
    else:
        print("Usage: iris obsidian {inbox,status}   (see --help)")
