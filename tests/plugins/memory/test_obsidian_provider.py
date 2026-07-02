import json

import pytest

from plugins.memory.obsidian import ObsidianMemoryProvider
from plugins.memory.obsidian.index import VaultIndex


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / ".obsidian").mkdir(parents=True)
    (v / "Journal").mkdir()
    (v / "Journal" / "2026-07-01.md").write_text(
        "# Daily\nWorked on the iris rollout with Alice.\n", encoding="utf-8"
    )
    (v / "Projects.md").write_text(
        "# Projects\n- iris: self-hosted stack with obsidian memory\n- other stuff\n",
        encoding="utf-8",
    )
    return v


@pytest.fixture
def provider(vault, tmp_path):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault)})
    p.initialize("sess-abc123", hermes_home=str(tmp_path), platform="cli")
    return p


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    """Fake built-in memory store on disk (what _regenerate_mirror reads)."""
    d = tmp_path / "memories"
    d.mkdir()
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: d)
    return d


# -- Availability / init -------------------------------------------------------


def test_is_available_requires_existing_vault(tmp_path):
    assert ObsidianMemoryProvider(config={}).is_available() is False
    missing = ObsidianMemoryProvider(config={"vault_path": str(tmp_path / "nope")})
    assert missing.is_available() is False
    ok = ObsidianMemoryProvider(config={"vault_path": str(tmp_path)})
    assert ok.is_available() is True


def test_default_folder_is_iris(provider, vault):
    assert (vault / "Iris").is_dir()


def test_initialize_without_vault_raises(tmp_path):
    p = ObsidianMemoryProvider(config={"vault_path": str(tmp_path / "missing")})
    with pytest.raises(RuntimeError):
        p.initialize("s1")


def test_loader_discovers_obsidian_plugin():
    from plugins.memory import find_provider_dir

    assert find_provider_dir("obsidian") is not None


def test_register_entry_point():
    from plugins.memory import obsidian

    class Ctx:
        provider = None

        def register_memory_provider(self, p):
            self.provider = p

    ctx = Ctx()
    obsidian.register(ctx)
    assert isinstance(ctx.provider, ObsidianMemoryProvider)


# -- FTS5 index ------------------------------------------------------------------


def test_index_is_active_and_incremental(provider, vault):
    assert provider._index is not None and provider._index.available

    hits = provider._search_vault("iris rollout")
    assert any("Journal" in h["path"] for h in hits)

    # New file appears after a forced refresh (mtime-based incremental pass).
    (vault / "Gardening.md").write_text("Tomatoes and zucchini beds.\n", encoding="utf-8")
    provider._index.refresh(force=True)
    hits = provider._search_vault("zucchini")
    assert [h["path"] for h in hits] == ["Gardening.md"]

    # Deleted file disappears from results.
    (vault / "Gardening.md").unlink()
    provider._index.refresh(force=True)
    assert provider._search_vault("zucchini") == []


def test_own_writes_searchable_immediately(provider):
    provider.handle_tool_call(
        "obsidian",
        {"action": "write", "title": "Quantum Plan", "content": "entangled qubits roadmap"},
    )
    hits = json.loads(
        provider.handle_tool_call("obsidian", {"action": "search", "query": "entangled qubits"})
    )
    assert hits["count"] == 1
    assert hits["results"][0]["path"] == "Iris/Notes/Quantum Plan.md"


def test_scan_fallback_when_index_unavailable(vault, tmp_path):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault)})
    p.initialize("s1", hermes_home=str(tmp_path))
    if p._index:
        p._index.close()
    p._index = None
    hits = p._search_vault("iris rollout")
    assert any("Journal" in h["path"] for h in hits)


def test_index_skips_vault_internals(provider, vault):
    (vault / ".obsidian" / "workspace.md").write_text("secretpane", encoding="utf-8")
    provider._index.refresh(force=True)
    assert provider._search_vault("secretpane") == []


# -- Tool: search / read / list ------------------------------------------------


def test_search_finds_notes_across_vault(provider):
    result = json.loads(
        provider.handle_tool_call("obsidian", {"action": "search", "query": "iris"})
    )
    assert result["count"] >= 2
    paths = {hit["path"] for hit in result["results"]}
    assert "Projects.md" in paths
    assert any("Journal" in p for p in paths)
    assert all("snippet" in hit for hit in result["results"])


def test_search_requires_query(provider):
    result = json.loads(provider.handle_tool_call("obsidian", {"action": "search"}))
    assert "error" in result


def test_read_note_with_and_without_md_extension(provider):
    for path in ("Projects.md", "Projects"):
        result = json.loads(
            provider.handle_tool_call("obsidian", {"action": "read", "path": path})
        )
        assert "iris" in result["content"]


def test_read_missing_note_errors(provider):
    result = json.loads(
        provider.handle_tool_call("obsidian", {"action": "read", "path": "Nope.md"})
    )
    assert "error" in result


def test_list_vault_and_subfolder(provider):
    all_notes = json.loads(provider.handle_tool_call("obsidian", {"action": "list"}))
    assert "Projects.md" in all_notes["notes"]
    journal = json.loads(
        provider.handle_tool_call("obsidian", {"action": "list", "path": "Journal"})
    )
    assert journal["notes"] == ["Journal/2026-07-01.md"]


# -- Tool: write / append / delete ----------------------------------------------


def test_write_creates_note_with_frontmatter(provider, vault):
    result = json.loads(
        provider.handle_tool_call(
            "obsidian",
            {
                "action": "write",
                "title": "Iris Plan",
                "content": "Self-hosted stack. See [[Projects]].",
                "tags": ["iris/plan"],
            },
        )
    )
    assert result["status"] == "written"
    text = (vault / "Iris" / "Notes" / "Iris Plan.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "source: iris" in text
    assert "tags: [iris/plan]" in text
    assert "[[Projects]]" in text


def test_append_creates_then_appends(provider, vault):
    provider.handle_tool_call(
        "obsidian", {"action": "append", "title": "Log", "content": "first"}
    )
    provider.handle_tool_call(
        "obsidian", {"action": "append", "title": "Log", "content": "second"}
    )
    text = (vault / "Iris" / "Notes" / "Log.md").read_text(encoding="utf-8")
    assert "first" in text and "second" in text
    assert text.index("first") < text.index("second")


def test_no_temp_files_left_in_vault(provider, vault):
    provider.handle_tool_call(
        "obsidian", {"action": "write", "title": "Clean", "content": "x"}
    )
    leftovers = [p for p in vault.rglob("*.tmp")]
    assert leftovers == []


def test_title_sanitization_blocks_traversal(provider, vault):
    json.loads(
        provider.handle_tool_call(
            "obsidian",
            {"action": "write", "title": "../../evil", "content": "x"},
        )
    )
    assert not (vault.parent / "evil.md").exists()
    assert not (vault / "evil.md").exists()
    assert list((vault / "Iris" / "Notes").glob("*.md"))


def test_delete_restricted_to_iris_folder(provider, vault):
    result = json.loads(
        provider.handle_tool_call("obsidian", {"action": "delete", "path": "Projects.md"})
    )
    assert "error" in result
    assert (vault / "Projects.md").exists()

    provider.handle_tool_call(
        "obsidian", {"action": "write", "title": "Scratch", "content": "tmp"}
    )
    result = json.loads(
        provider.handle_tool_call(
            "obsidian", {"action": "delete", "path": "Iris/Notes/Scratch.md"}
        )
    )
    assert result["status"] == "deleted"


def test_read_rejects_path_escaping_vault(provider, tmp_path):
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    result = json.loads(
        provider.handle_tool_call("obsidian", {"action": "read", "path": "../secret.md"})
    )
    assert "error" in result


# -- Auto-linking ------------------------------------------------------------------


def test_auto_link_wraps_existing_titles(provider, vault):
    provider._search_vault("warm the index")  # first refresh indexes the vault
    provider.handle_tool_call(
        "obsidian",
        {"action": "write", "title": "Roadmap", "content": "Ship Projects milestone one."},
    )
    text = (vault / "Iris" / "Notes" / "Roadmap.md").read_text(encoding="utf-8")
    assert "[[Projects]]" in text


def test_auto_link_preserves_casing_and_skips_code(provider, vault):
    provider._search_vault("warm the index")
    provider.handle_tool_call(
        "obsidian",
        {
            "action": "write",
            "title": "Casing",
            "content": "about projects here\n```\nprojects in code\n```",
        },
    )
    text = (vault / "Iris" / "Notes" / "Casing.md").read_text(encoding="utf-8")
    assert "[[Projects|projects]]" in text
    assert "```\nprojects in code\n```" in text  # code fence untouched


def test_auto_link_respects_existing_links(provider):
    provider._search_vault("warm the index")
    linked = provider._auto_link("Already [[Projects]] linked, and Projects again.")
    # First occurrence is already a link; the second stays plain (title already linked once).
    assert linked.count("[[Projects]]") == 1


def test_auto_link_disabled(vault, tmp_path):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault), "auto_link": "false"})
    p.initialize("s1", hermes_home=str(tmp_path))
    assert p._auto_link("Ship Projects milestone") == "Ship Projects milestone"


# -- Prefetch / system prompt ----------------------------------------------------


def test_prefetch_returns_relevant_snippets(provider):
    context = provider.prefetch("what is the iris project?")
    assert "Obsidian Memory" in context
    assert "iris" in context.lower()
    assert ".md]]" not in context  # wikilinks in context drop the extension


def test_prefetch_empty_query_and_no_match(provider):
    assert provider.prefetch("") == ""
    assert provider.prefetch("zzz qqq xxx nomatch") == ""


def test_queue_prefetch_caches_result(provider):
    provider.queue_prefetch("iris rollout")
    import time

    for _ in range(50):
        with provider._prefetch_lock:
            if provider._prefetch_result:
                break
        time.sleep(0.05)
    assert "iris" in provider.prefetch("iris rollout").lower()


def test_system_prompt_block_mentions_folder(provider):
    block = provider.system_prompt_block()
    assert "Obsidian Memory" in block
    assert "Iris" in block


# -- Built-in memory mirroring -----------------------------------------------------


def _write_builtin(memory_dir, name, entries):
    memory_dir.joinpath(name).write_text("\n§\n".join(entries), encoding="utf-8")


def test_mirror_add_replace_remove_stay_in_sync(provider, vault, memory_dir):
    _write_builtin(memory_dir, "MEMORY.md", ["User prefers vim keybindings"])
    provider.on_memory_write("add", "memory", "User prefers vim keybindings")
    mirror = vault / "Iris" / "Memory.md"
    assert "vim keybindings" in mirror.read_text(encoding="utf-8")

    # replace: mirror reflects the new on-disk state, old entry is gone
    _write_builtin(memory_dir, "MEMORY.md", ["User prefers helix keybindings"])
    provider.on_memory_write("replace", "memory", "User prefers helix keybindings")
    text = mirror.read_text(encoding="utf-8")
    assert "helix" in text and "vim" not in text

    # remove: mirror empties out instead of accumulating stale entries
    _write_builtin(memory_dir, "MEMORY.md", [])
    provider.on_memory_write("remove", "memory", "User prefers helix keybindings")
    text = mirror.read_text(encoding="utf-8")
    assert "helix" not in text
    assert "empty" in text


def test_mirror_user_target(provider, vault, memory_dir):
    _write_builtin(memory_dir, "USER.md", ["Name is Louan", "Speaks French"])
    provider.on_memory_write("add", "user", "Speaks French")
    text = (vault / "Iris" / "User Profile.md").read_text(encoding="utf-8")
    assert "Louan" in text and "French" in text
    assert "Iris" in text  # attribution header


def test_mirror_skips_when_nothing_to_mirror(provider, vault, memory_dir):
    provider.on_memory_write("add", "memory", "whatever")  # no MEMORY.md on disk
    assert not (vault / "Iris" / "Memory.md").exists()


def test_non_primary_context_never_writes(vault, tmp_path, memory_dir):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault)})
    p.initialize("cron-1", agent_context="cron", hermes_home=str(tmp_path))
    _write_builtin(memory_dir, "MEMORY.md", ["cron noise"])
    p.on_memory_write("add", "memory", "cron noise")
    p.on_session_end([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert not (vault / "Iris" / "Memory.md").exists()
    assert not (vault / "Iris" / "Sessions").exists()


# -- Session notes --------------------------------------------------------------------


_EXCHANGE = [
    {"role": "user", "content": "Set up the iris stack"},
    {"role": "assistant", "content": "Done — Ollama is running locally."},
    {"role": "user", "content": [{"type": "text", "text": "non-string ignored"}]},
]


def test_session_note_written_on_session_end(provider, vault, monkeypatch):
    monkeypatch.setattr(provider, "_summarize_session", lambda exchanges: "")
    provider.on_session_end(_EXCHANGE)
    notes = list((vault / "Iris" / "Sessions").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert "iris stack" in text
    assert "**Iris:**" in text
    assert "tags: [iris/session]" in text


def test_session_note_uses_llm_summary_when_available(provider, vault, monkeypatch):
    monkeypatch.setattr(
        provider, "_summarize_session", lambda exchanges: "- Decided on Ollama"
    )
    provider.on_session_end(_EXCHANGE)
    text = next((vault / "Iris" / "Sessions").glob("*.md")).read_text(encoding="utf-8")
    assert "## Summary" in text
    assert "- Decided on Ollama" in text
    assert "## Transcript" in text


def test_summarize_session_falls_back_on_llm_failure(provider, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("aux client down")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", boom)
    assert provider._summarize_session([("hi", "hello")]) == ""


def test_summarize_session_uses_llm_response(provider, monkeypatch):
    class Msg:
        content = "- point one\n- point two"

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: Resp())
    long_exchange = [("tell me about the project " * 10, "it is going well " * 10)]
    assert provider._summarize_session(long_exchange) == "- point one\n- point two"


def test_summary_off_skips_llm(vault, tmp_path, monkeypatch):
    p = ObsidianMemoryProvider(
        config={"vault_path": str(vault), "session_summary": "off"}
    )
    p.initialize("s1", hermes_home=str(tmp_path))

    def boom(**kwargs):
        raise AssertionError("must not be called")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", boom)
    assert p._summarize_session([("hi", "hello")]) == ""


def test_session_note_disabled(vault, tmp_path, monkeypatch):
    p = ObsidianMemoryProvider(
        config={"vault_path": str(vault), "session_notes": "false"}
    )
    p.initialize("s1", hermes_home=str(tmp_path))
    monkeypatch.setattr(p, "_summarize_session", lambda exchanges: "")
    p.on_session_end(_EXCHANGE)
    assert not (vault / "Iris" / "Sessions").exists()


def test_session_note_skipped_when_empty(provider, vault):
    provider.on_session_end([])
    assert not (vault / "Iris" / "Sessions").exists()


def test_on_session_switch_updates_session_id(provider):
    provider.on_session_switch("new-session", reset=True)
    assert provider._session_id == "new-session"


# -- Daily notes ------------------------------------------------------------------------


def test_daily_note_appended_with_session_section(vault, tmp_path, monkeypatch):
    p = ObsidianMemoryProvider(
        config={
            "vault_path": str(vault),
            "daily_notes": "true",
            "daily_notes_folder": "Journal",
        }
    )
    p.initialize("s1", hermes_home=str(tmp_path))
    monkeypatch.setattr(p, "_summarize_session", lambda exchanges: "- summary line")
    p.on_session_end(_EXCHANGE)

    from datetime import datetime

    daily = vault / "Journal" / (datetime.now().strftime("%Y-%m-%d") + ".md")
    text = daily.read_text(encoding="utf-8")
    assert "## Iris — " in text
    assert "- summary line" in text
    assert "Full note: [[Iris/Sessions/" in text


def test_daily_note_appends_to_existing_note(vault, tmp_path, monkeypatch):
    from datetime import datetime

    daily = vault / (datetime.now().strftime("%Y-%m-%d") + ".md")
    daily.write_text("# my day\nmorning thoughts\n", encoding="utf-8")
    p = ObsidianMemoryProvider(
        config={"vault_path": str(vault), "daily_notes": "true", "session_notes": "false"}
    )
    p.initialize("s1", hermes_home=str(tmp_path))
    monkeypatch.setattr(p, "_summarize_session", lambda exchanges: "")
    p.on_session_end(_EXCHANGE)
    text = daily.read_text(encoding="utf-8")
    assert text.startswith("# my day\nmorning thoughts")
    assert "## Iris — " in text


def test_daily_notes_off_by_default(provider, vault, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(provider, "_summarize_session", lambda exchanges: "")
    provider.on_session_end(_EXCHANGE)
    assert not (vault / (datetime.now().strftime("%Y-%m-%d") + ".md")).exists()


# -- Config schema ----------------------------------------------------------------------


def test_config_schema_fields(provider):
    schema = provider.get_config_schema()
    keys = {f["key"] for f in schema}
    assert {"vault_path", "folder", "session_notes", "session_summary", "daily_notes"} <= keys
    vault_field = next(f for f in schema if f["key"] == "vault_path")
    assert vault_field.get("required") is True
    folder_field = next(f for f in schema if f["key"] == "folder")
    assert folder_field["default"] == "Iris"
    assert not any(f.get("secret") for f in schema)


def test_save_config_writes_yaml(provider, tmp_path):
    provider.save_config({"vault_path": "/v", "folder": "H"}, str(tmp_path))
    import yaml

    with open(tmp_path / "config.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["plugins"]["obsidian-memory"]["vault_path"] == "/v"


def test_tool_schema_shape(provider):
    schemas = provider.get_tool_schemas()
    assert len(schemas) == 1
    assert schemas[0]["name"] == "obsidian"
    actions = schemas[0]["parameters"]["properties"]["action"]["enum"]
    assert set(actions) == {"search", "read", "list", "write", "append", "delete"}
