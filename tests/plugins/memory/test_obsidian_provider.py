import json

import pytest

from plugins.memory.obsidian import ObsidianMemoryProvider


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / ".obsidian").mkdir(parents=True)
    (v / "Journal").mkdir()
    (v / "Journal" / "2026-07-01.md").write_text(
        "# Daily\nWorked on the iris rollout with Alice.\n", encoding="utf-8"
    )
    (v / "Projects.md").write_text(
        "# Projects\n- iris: self-hosted hermes stack\n- other stuff\n",
        encoding="utf-8",
    )
    return v


@pytest.fixture
def provider(vault):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault), "folder": "Hermes"})
    p.initialize("sess-abc123", hermes_home=str(vault.parent), platform="cli")
    return p


# -- Availability / init -------------------------------------------------------


def test_is_available_requires_existing_vault(tmp_path):
    assert ObsidianMemoryProvider(config={}).is_available() is False
    missing = ObsidianMemoryProvider(config={"vault_path": str(tmp_path / "nope")})
    assert missing.is_available() is False
    ok = ObsidianMemoryProvider(config={"vault_path": str(tmp_path)})
    assert ok.is_available() is True


def test_initialize_creates_hermes_folder(provider, vault):
    assert (vault / "Hermes").is_dir()


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
                "tags": ["hermes/plan"],
            },
        )
    )
    assert result["status"] == "written"
    note = vault / "Hermes" / "Notes" / "Iris Plan.md"
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "tags: [hermes/plan]" in text
    assert "[[Projects]]" in text


def test_append_creates_then_appends(provider, vault):
    provider.handle_tool_call(
        "obsidian", {"action": "append", "title": "Log", "content": "first"}
    )
    provider.handle_tool_call(
        "obsidian", {"action": "append", "title": "Log", "content": "second"}
    )
    text = (vault / "Hermes" / "Notes" / "Log.md").read_text(encoding="utf-8")
    assert "first" in text and "second" in text
    assert text.index("first") < text.index("second")


def test_title_sanitization_blocks_traversal(provider, vault):
    json.loads(
        provider.handle_tool_call(
            "obsidian",
            {"action": "write", "title": "../../evil", "content": "x"},
        )
    )
    assert not (vault.parent / "evil.md").exists()
    assert not (vault / "evil.md").exists()
    # The sanitized note landed inside the Hermes Notes folder.
    assert list((vault / "Hermes" / "Notes").glob("*.md"))


def test_delete_restricted_to_hermes_folder(provider, vault):
    # User note outside the Hermes folder: refused.
    result = json.loads(
        provider.handle_tool_call("obsidian", {"action": "delete", "path": "Projects.md"})
    )
    assert "error" in result
    assert (vault / "Projects.md").exists()

    # Agent-created note inside the Hermes folder: allowed.
    provider.handle_tool_call(
        "obsidian", {"action": "write", "title": "Scratch", "content": "tmp"}
    )
    result = json.loads(
        provider.handle_tool_call(
            "obsidian", {"action": "delete", "path": "Hermes/Notes/Scratch.md"}
        )
    )
    assert result["status"] == "deleted"


def test_read_rejects_path_escaping_vault(provider, tmp_path):
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    result = json.loads(
        provider.handle_tool_call("obsidian", {"action": "read", "path": "../secret.md"})
    )
    assert "error" in result


# -- Prefetch / system prompt ----------------------------------------------------


def test_prefetch_returns_relevant_snippets(provider):
    context = provider.prefetch("what is the iris project?")
    assert "Obsidian Memory" in context
    assert "iris" in context.lower()


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
    assert "Hermes" in block


# -- Built-in memory mirroring -----------------------------------------------------


def test_on_memory_write_mirrors_add(provider, vault):
    provider.on_memory_write("add", "memory", "User prefers vim keybindings")
    provider.on_memory_write("add", "user", "Name is Louan")
    memory = (vault / "Hermes" / "Memory.md").read_text(encoding="utf-8")
    profile = (vault / "Hermes" / "User Profile.md").read_text(encoding="utf-8")
    assert "vim keybindings" in memory
    assert "Louan" in profile


def test_on_memory_write_ignores_remove(provider, vault):
    provider.on_memory_write("remove", "memory", "gone")
    assert not (vault / "Hermes" / "Memory.md").exists()


def test_non_primary_context_never_writes(vault):
    p = ObsidianMemoryProvider(config={"vault_path": str(vault)})
    p.initialize("cron-1", agent_context="cron")
    p.on_memory_write("add", "memory", "cron noise")
    p.on_session_end([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert not (vault / "Hermes" / "Memory.md").exists()
    assert not (vault / "Hermes" / "Sessions").exists()


# -- Session notes --------------------------------------------------------------------


def test_session_note_written_on_session_end(provider, vault):
    provider.on_session_end([
        {"role": "user", "content": "Set up the iris stack"},
        {"role": "assistant", "content": "Done — Ollama is running locally."},
        {"role": "user", "content": [{"type": "text", "text": "non-string ignored"}]},
    ])
    notes = list((vault / "Hermes" / "Sessions").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert "iris stack" in text
    assert "Ollama" in text
    assert "tags: [hermes/session]" in text


def test_session_note_disabled(vault):
    p = ObsidianMemoryProvider(
        config={"vault_path": str(vault), "session_notes": "false"}
    )
    p.initialize("s1")
    p.on_session_end([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert not (vault / "Hermes" / "Sessions").exists()


def test_session_note_skipped_when_empty(provider, vault):
    provider.on_session_end([])
    assert not (vault / "Hermes" / "Sessions").exists()


def test_on_session_switch_updates_session_id(provider):
    provider.on_session_switch("new-session", reset=True)
    assert provider._session_id == "new-session"


# -- Config schema ----------------------------------------------------------------------


def test_config_schema_has_required_vault_path(provider):
    schema = provider.get_config_schema()
    keys = {f["key"] for f in schema}
    assert {"vault_path", "folder", "session_notes"} <= keys
    vault_field = next(f for f in schema if f["key"] == "vault_path")
    assert vault_field.get("required") is True
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
