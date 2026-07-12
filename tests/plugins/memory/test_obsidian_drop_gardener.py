import datetime as dt
import os
import time

import pytest

from plugins.memory.obsidian.drop import DropConfig, DropProcessor, build_drop_prompt
from plugins.memory.obsidian.gardener import Gardener, GardenerConfig
from plugins.memory.obsidian.inbox import parse_note


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / ".obsidian").mkdir(parents=True)
    return v


def make_drop(vault, tmp_path, runner=None, **cfg):
    config = DropConfig.from_plugin_config(
        {"vault_path": str(vault), **cfg}, hermes_home=str(tmp_path / "home")
    )
    runner = runner or (lambda p, t: (True, "Summary of the file. See [[Projects]]. #reading"))
    return DropProcessor(config, run_task=runner)


def make_gardener(vault, tmp_path, runner=None, **cfg):
    config = GardenerConfig.from_plugin_config(
        {"vault_path": str(vault), **cfg}, hermes_home=str(tmp_path / "home")
    )
    runner = runner or (lambda p, t: (True, "## Connections\n- [[A]] ↔ [[B]] — same topic"))
    return Gardener(config, run_task=runner)


# -- Drop: prompt building ---------------------------------------------------------


def test_drop_prompt_inlines_small_text(vault):
    f = vault / "note.txt"
    f.write_text("the content of the dropped file", encoding="utf-8")
    p = build_drop_prompt(f, "Iris/Notes")
    assert "the content of the dropped file" in p
    assert "note.txt" in p


def test_drop_prompt_references_binaries_by_path(vault):
    f = vault / "scan.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    p = build_drop_prompt(f, "Iris/Notes")
    assert str(f) in p
    assert "vision_analyze" in p or "read_file" in p


# -- Drop: lifecycle ------------------------------------------------------------------


def test_drop_file_becomes_note_and_archives(vault, tmp_path):
    d = make_drop(vault, tmp_path)
    drop_dir = d.config.drop_dir
    drop_dir.mkdir(parents=True)
    (drop_dir / "meeting notes.txt").write_text("raw meeting scribbles", encoding="utf-8")

    res = d.run_once()
    assert res.processed == ["meeting notes.txt"]

    note = vault / "Iris" / "Notes" / "Meeting notes.md"
    fm, body = parse_note(note.read_text(encoding="utf-8"))
    assert fm["source"] == "iris/drop"
    assert fm["original"] == "meeting notes.txt"
    assert "[[Projects]]" in body

    # original archived, journal updated, second pass idle
    assert not (drop_dir / "meeting notes.txt").exists()
    assert (drop_dir / "Archive" / "meeting notes.txt").exists()
    journal = (drop_dir / "Journal.md").read_text(encoding="utf-8")
    assert "meeting notes.txt → [[Iris/Notes/Meeting notes]]" in journal
    assert d.run_once().processed == []


def test_drop_name_collision_gets_unique_note(vault, tmp_path):
    d = make_drop(vault, tmp_path)
    d.config.drop_dir.mkdir(parents=True)
    (vault / "Iris" / "Notes").mkdir(parents=True)
    (vault / "Iris" / "Notes" / "Report.md").write_text("existing", encoding="utf-8")
    (d.config.drop_dir / "report.txt").write_text("x", encoding="utf-8")
    d.run_once()
    assert (vault / "Iris" / "Notes" / "Report 2.md").exists()
    assert (vault / "Iris" / "Notes" / "Report.md").read_text(encoding="utf-8") == "existing"


def test_drop_failure_leaves_file_with_backoff(vault, tmp_path):
    d = make_drop(vault, tmp_path, runner=lambda p, t: (False, "cannot read"))
    d.config.drop_dir.mkdir(parents=True)
    f = d.config.drop_dir / "broken.pdf"
    f.write_bytes(b"junk")

    res = d.run_once()
    assert res.failed == ["broken.pdf"]
    assert f.exists()                      # left in place
    assert "✗ broken.pdf" in (d.config.drop_dir / "Journal.md").read_text(encoding="utf-8")
    # cooldown: immediately retrying is skipped
    assert d.run_once().failed == []
    assert d.find_pending() == []


def test_drop_gives_up_after_max_attempts(vault, tmp_path, monkeypatch):
    d = make_drop(vault, tmp_path, runner=lambda p, t: (False, "nope"))
    d.config.drop_dir.mkdir(parents=True)
    (d.config.drop_dir / "cursed.bin").write_bytes(b"x")
    t = [time.time() - 10 * 3600]

    def fake_time():
        return t[0]

    monkeypatch.setattr("plugins.memory.obsidian.drop.time.time", fake_time)
    monkeypatch.setattr("plugins.memory.obsidian.inbox.time.time", fake_time)
    for _ in range(3):
        d.run_once()
        t[0] += 2 * 3600                   # past the cooldown each time
    # three strikes — permanently skipped
    assert d.find_pending() == []


def test_drop_ignores_partials_dotfiles_and_journal(vault, tmp_path):
    d = make_drop(vault, tmp_path)
    d.config.drop_dir.mkdir(parents=True)
    (d.config.drop_dir / ".hidden").write_text("x", encoding="utf-8")
    (d.config.drop_dir / "download.part").write_text("x", encoding="utf-8")
    (d.config.drop_dir / "Journal.md").write_text("# Drop Journal", encoding="utf-8")
    assert d.find_pending() == []


def test_drop_shares_rate_limit_budget(vault, tmp_path):
    d = make_drop(vault, tmp_path, inbox_max_per_hour=1)
    d.config.drop_dir.mkdir(parents=True)
    (d.config.drop_dir / "a.txt").write_text("a", encoding="utf-8")
    (d.config.drop_dir / "b.txt").write_text("b", encoding="utf-8")
    res = d.run_once()
    assert len(res.processed) == 1 and res.rate_limited == 1


def test_drop_custom_folder(vault, tmp_path):
    d = make_drop(vault, tmp_path, drop_folder="Boite")
    assert d.config.drop_dir == vault / "Boite"


# -- Gardener --------------------------------------------------------------------------


def _touch_note(vault, rel, text="# Note\ncontent", age_days=0):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


def test_gardener_writes_dated_briefing(vault, tmp_path):
    _touch_note(vault, "Projects.md", "# Projects\n- iris stack")
    _touch_note(vault, "Journal/today.md", "worked on the rollout")
    g = make_gardener(vault, tmp_path)
    now = dt.datetime(2026, 7, 12, 5, 0)
    path = g.run(now=now)
    assert path == vault / "Iris" / "Briefings" / "2026-07-12.md"
    fm, body = parse_note(path.read_text(encoding="utf-8"))
    assert fm["source"] == "iris/gardener"
    assert "Morning briefing — 2026-07-12" in body
    assert "[[A]] ↔ [[B]]" in body


def test_gardener_prompt_lists_recent_notes_only(vault, tmp_path):
    _touch_note(vault, "Fresh.md", "# Fresh\nnew stuff")
    _touch_note(vault, "Ancient.md", "# Ancient\nold stuff", age_days=30)
    g = make_gardener(vault, tmp_path, gardener_days=3)
    prompt = g.build_prompt()
    assert "[[Fresh]]" in prompt
    assert "[[Ancient]]" not in prompt


def test_gardener_ignores_own_briefings(vault, tmp_path):
    _touch_note(vault, "Iris/Briefings/2026-07-11.md", "# old briefing")
    g = make_gardener(vault, tmp_path)
    assert g.build_prompt() is None        # nothing else recent → skip


def test_gardener_once_per_day_guard(vault, tmp_path):
    _touch_note(vault, "N.md")
    calls = []

    def runner(p, t):
        calls.append(1)
        return True, "briefing"

    g = make_gardener(vault, tmp_path, runner=runner)
    now = dt.datetime(2026, 7, 12, 5, 0)
    assert g.run(now=now) is not None
    assert g.run(now=now.replace(hour=6)) is None      # same day → skip
    assert len(calls) == 1
    # force runs again and versions the file instead of clobbering
    assert g.run(force=True, now=now.replace(hour=7)) is not None
    assert len(calls) == 2
    briefings = sorted((vault / "Iris" / "Briefings").glob("*.md"))
    assert len(briefings) == 2


def test_gardener_failed_session_writes_nothing(vault, tmp_path):
    _touch_note(vault, "N.md")
    g = make_gardener(vault, tmp_path, runner=lambda p, t: (False, "api down"))
    assert g.run() is None
    assert not (vault / "Iris" / "Briefings").exists()
    # and the day is NOT marked done — the next scheduled attempt retries
    assert not g.already_ran_today()


# -- CLI wiring ---------------------------------------------------------------------


def test_cli_registers_new_subcommands():
    import argparse

    from plugins.memory.obsidian.cli import register_cli

    parser = argparse.ArgumentParser()
    register_cli(parser)
    assert parser.parse_args(["drop", "--once"]).obsidian_command == "drop"
    assert parser.parse_args(["gardener", "--force"]).force is True
    args = parser.parse_args(["watch", "--gardener-hour", "4"])
    assert args.obsidian_command == "watch" and args.gardener_hour == 4
