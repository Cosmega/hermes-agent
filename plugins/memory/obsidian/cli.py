"""CLI commands for the Obsidian memory plugin.

Registered as ``iris obsidian ...`` while obsidian is the active memory
provider (the plugin CLI system only loads the active provider's cli.py).

Commands:
    iris obsidian inbox            # watch the vault for #iris/task notes
    iris obsidian inbox --once     # single scan-and-process pass (cron-friendly)
    iris obsidian drop --once      # process files dropped in <folder>/Drop/
    iris obsidian gardener         # nightly gardening pass → morning briefing
    iris obsidian watch            # combined daemon: inbox + drop + gardener
    iris obsidian status           # show config and recent activity
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


def _build_drop():
    from hermes_constants import get_hermes_home

    from .drop import DropConfig, DropProcessor

    cfg = _plugin_config()
    if not cfg.get("vault_path"):
        print("✗ obsidian memory is not configured — run 'iris memory setup' first.")
        return None
    drop_cfg = DropConfig.from_plugin_config(cfg, hermes_home=str(get_hermes_home()))
    if not drop_cfg.vault_path.is_dir():
        print(f"✗ vault not found: {drop_cfg.vault_path}")
        return None
    return DropProcessor(drop_cfg)


def _build_gardener():
    from hermes_constants import get_hermes_home

    from .gardener import Gardener, GardenerConfig

    cfg = _plugin_config()
    if not cfg.get("vault_path"):
        print("✗ obsidian memory is not configured — run 'iris memory setup' first.")
        return None
    g_cfg = GardenerConfig.from_plugin_config(cfg, hermes_home=str(get_hermes_home()))
    if not g_cfg.vault_path.is_dir():
        print(f"✗ vault not found: {g_cfg.vault_path}")
        return None
    return Gardener(g_cfg)


def _print_drop_result(res, indent="  ") -> None:
    for name in res.processed:
        print(f"{indent}✦ filed: {name}")
    for name in res.failed:
        print(f"{indent}✗ failed: {name}")
    if res.rate_limited:
        print(f"{indent}⏳ rate-limited: {res.rate_limited} file(s) deferred")


def _cmd_drop(args) -> None:
    processor = _build_drop()
    if processor is None:
        sys.exit(1)
    d = processor.config.drop_dir
    d.mkdir(parents=True, exist_ok=True)
    print(f"✦ Iris Drop — folder: {d}")
    if getattr(args, "once", False):
        res = processor.run_once()
        print(f"  processed: {len(res.processed)} · failed: {len(res.failed)} · "
              f"deferred: {res.rate_limited}")
        _print_drop_result(res)
        sys.exit(0 if not res.failed else 1)
    interval = max(5.0, float(getattr(args, "interval", 30.0)))
    print(f"  watching every {interval:.0f}s — drop files in, notes come out")
    try:
        while True:
            _print_drop_result(processor.run_once(), indent="")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n✦ drop watcher stopped")


def _cmd_gardener(args) -> None:
    gardener = _build_gardener()
    if gardener is None:
        sys.exit(1)
    force = getattr(args, "force", False)
    if not force and gardener.already_ran_today():
        print("✦ gardener already ran today — use --force to run again")
        return
    print("✦ Iris Gardener — reviewing the vault… (this runs a full agent session)")
    path = gardener.run(force=force)
    if path is None:
        print("  nothing to garden (no recent notes) or the session failed — see logs")
        return
    rel = path.relative_to(gardener.config.vault_path)
    print(f"  ✦ morning briefing written: [[{str(rel)[:-3]}]]")


def _cmd_watch(args) -> None:
    watcher = _build_watcher()
    processor = _build_drop()
    gardener = _build_gardener()
    if watcher is None or processor is None:
        sys.exit(1)
    processor.config.drop_dir.mkdir(parents=True, exist_ok=True)
    interval = max(5.0, float(getattr(args, "interval", 30.0)))
    g_hour = int(getattr(args, "gardener_hour", 5))
    print(f"✦ Iris watch — inbox + drop every {interval:.0f}s, "
          f"gardener at {g_hour:02d}:00 (vault: {watcher.config.vault_path})")
    import datetime as dt
    try:
        while True:
            res = watcher.run_once()
            for rel in res.ran:
                print(f"✦ task done: {rel}")
            for rel in res.failed:
                print(f"✗ task failed: {rel}")
            for rel in res.blocked:
                print(f"⛔ task blocked: {rel}")
            _print_drop_result(processor.run_once(), indent="")
            if gardener and dt.datetime.now().hour == g_hour \
                    and not gardener.already_ran_today():
                path = gardener.run()
                if path:
                    print(f"✦ morning briefing: {path.name}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n✦ watch stopped")


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

    drop = subs.add_parser(
        "drop",
        help="Turn files dropped in <folder>/Drop/ into wikilinked notes",
    )
    drop.add_argument("--once", action="store_true",
                      help="Single pass, then exit (cron-friendly)")
    drop.add_argument("--interval", type=float, default=30.0,
                      help="Seconds between scans in watch mode (default 30)")

    gardener = subs.add_parser(
        "gardener",
        help="Nightly gardening pass — writes a dated morning briefing note",
    )
    gardener.add_argument("--force", action="store_true",
                          help="Run even if a pass already ran today")

    watch = subs.add_parser(
        "watch",
        help="Combined daemon: inbox tasks + drop folder + nightly gardener",
    )
    watch.add_argument("--interval", type=float, default=30.0,
                       help="Seconds between scans (default 30)")
    watch.add_argument("--gardener-hour", type=int, default=5, dest="gardener_hour",
                       help="Hour of day (0-23) for the gardening pass (default 5)")

    subs.add_parser("status", help="Show obsidian memory + inbox status")


def obsidian_command(args) -> None:
    """Route ``iris obsidian`` subcommands."""
    sub = getattr(args, "obsidian_command", None)
    if sub == "inbox":
        _cmd_inbox(args)
    elif sub == "drop":
        _cmd_drop(args)
    elif sub == "gardener":
        _cmd_gardener(args)
    elif sub == "watch":
        _cmd_watch(args)
    elif sub == "status":
        _cmd_status(args)
    else:
        print("Usage: iris obsidian {inbox,drop,gardener,watch,status}   (see --help)")
