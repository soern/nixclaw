"""Tests for src/nixclaw_agent/prompts.py (loader, merge, validation, render) and the soul hierarchy."""

import logging
from pathlib import Path

import pytest

from src.nixclaw_agent import prompts
from src.nixclaw_agent.config import config
from src.nixclaw_agent.prompts import PromptError, PromptSpec
from src.nixclaw_agent.soul import soul_manager


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    # Loader tests use a minimal registry (just "soul") so the temp dirs need
    # only soul.md; the shipped registry is exercised in the tests at the bottom.
    monkeypatch.setattr(prompts, "REGISTRY", (PromptSpec("soul", "soul.md"),))
    monkeypatch.setattr(prompts, "EXTRA_YAML_FILES", ())
    default = tmp_path / "default"
    custom = tmp_path / "custom"
    default.mkdir()
    custom.mkdir()
    (default / "soul.md").write_text("default soul\n")
    return default, custom


def _registry(monkeypatch, *specs, extra_yaml=()):
    monkeypatch.setattr(prompts, "REGISTRY", tuple(specs))
    monkeypatch.setattr(prompts, "EXTRA_YAML_FILES", tuple(extra_yaml))


def test_real_default_dir_loads():
    assert prompts.load_prompts(prompts.DEFAULT_DIR, Path("/nonexistent")).text("soul").lower().startswith("# soul.md")


def test_custom_overrides_default_and_is_stripped(dirs):
    default, custom = dirs
    (custom / "soul.md").write_text("\n custom soul \n\n")
    assert prompts.load_prompts(default, custom).text("soul") == "custom soul"


def test_missing_custom_is_silent_and_debug_names_file(dirs, caplog):
    default, custom = dirs
    with caplog.at_level(logging.DEBUG, logger=prompts.LOG.name):
        ps = prompts.load_prompts(default, custom / "does-not-exist")
    assert ps.text("soul") == "default soul"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(default / "soul.md") in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_missing_default_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "REGISTRY", (PromptSpec("soul", "soul.md"),))
    monkeypatch.setattr(prompts, "EXTRA_YAML_FILES", ())
    (tmp_path / "default").mkdir()
    with pytest.raises(PromptError) as ei:
        prompts.load_prompts(tmp_path / "default", tmp_path / "custom")
    assert "soul.md" in ei.value.errors[0]


def test_unknown_placeholder_is_error(dirs, monkeypatch):
    default, custom = dirs
    _registry(monkeypatch, PromptSpec("soul", "soul.md", placeholders=frozenset({"agent_name"})))
    (custom / "soul.md").write_text("hi {{agent_name}} {{bogus}}")
    with pytest.raises(PromptError, match="bogus"):
        prompts.load_prompts(default, custom)


def test_omitted_placeholder_is_fine(dirs, monkeypatch):
    default, custom = dirs
    _registry(monkeypatch, PromptSpec("soul", "soul.md", placeholders=frozenset({"agent_name"})))
    ps = prompts.load_prompts(default, custom)
    assert ps.render("soul") == "default soul"


def test_required_token_missing_is_error_and_errors_are_collected(dirs, monkeypatch):
    default, custom = dirs
    _registry(
        monkeypatch,
        PromptSpec("soul", "soul.md", required=("__MUTE__",)),
        PromptSpec("other", "other.md"),
    )
    with pytest.raises(PromptError) as ei:
        prompts.load_prompts(default, custom)
    joined = " | ".join(ei.value.errors)
    assert "__MUTE__" in joined and "other.md" in joined  # both problems reported at once


def test_render_is_single_pass(dirs, monkeypatch):
    default, custom = dirs
    _registry(monkeypatch, PromptSpec("soul", "soul.md", placeholders=frozenset({"name", "timezone"})))
    (default / "soul.md").write_text("A {{name}} B")
    ps = prompts.load_prompts(default, custom)
    assert ps.render("soul", name="literal {{timezone}}", timezone="UTC") == "A literal {{timezone}} B"


def test_yaml_leaf_merge_and_unknown_key(dirs, monkeypatch):
    default, custom = dirs
    _registry(monkeypatch, extra_yaml=("other-doc.yaml",))
    (default / "other-doc.yaml").write_text(
        "bash:\n  description: run bash\n  params:\n    cmd: the command\n    cwd: the dir\n"
    )
    (custom / "other-doc.yaml").write_text("bash:\n  params:\n    cmd: my command\n")
    doc = prompts.load_prompts(default, custom).doc("other-doc.yaml")
    assert doc["bash"] == {"description": "run bash", "params": {"cmd": "my command", "cwd": "the dir"}}

    (custom / "other-doc.yaml").write_text("bash:\n  params:\n    nope: x\n")
    with pytest.raises(PromptError, match="nope"):
        prompts.load_prompts(default, custom)
    (custom / "other-doc.yaml").write_text("nosuchtool:\n  description: x\n")
    with pytest.raises(PromptError, match="nosuchtool"):
        prompts.load_prompts(default, custom)


def test_yaml_registered_key(dirs, monkeypatch):
    default, custom = dirs
    _registry(monkeypatch, PromptSpec("greeting", "user-messages.yaml", key="greeting", placeholders=frozenset({"n"})))
    (default / "user-messages.yaml").write_text("greeting: hello {{n}}\n")
    (custom / "user-messages.yaml").write_text("greeting: hey {{n}}\n")
    assert prompts.load_prompts(default, custom).render("greeting", n=3) == "hey 3"
    (custom / "user-messages.yaml").write_text("typo: x\n")
    with pytest.raises(PromptError, match="typo"):
        prompts.load_prompts(default, custom)


def test_unknown_custom_file_is_debug_only_not_error(dirs, caplog):
    default, custom = dirs
    (custom / "sould.md").write_text("typo")
    (custom / ".gitkeep").write_text("")
    with caplog.at_level(logging.DEBUG, logger=prompts.LOG.name):
        ps = prompts.load_prompts(default, custom)
    assert ps.text("soul") == "default soul"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("sould.md" in m for m in debug)
    assert not any(".gitkeep" in m for m in debug)


def test_invalid_utf8_custom_md_is_prompt_error(dirs):
    default, custom = dirs
    (custom / "soul.md").write_bytes(b"\xff\xfe\x80")
    with pytest.raises(PromptError, match="soul.md"):
        prompts.load_prompts(default, custom)


def test_invalid_utf8_custom_yaml_is_prompt_error(dirs, monkeypatch):
    default, custom = dirs
    _registry(monkeypatch, extra_yaml=("other-doc.yaml",))
    (default / "other-doc.yaml").write_text("bash:\n  description: run bash\n")
    (custom / "other-doc.yaml").write_bytes(b"bash:\n  description: \xff\xfe\n")
    with pytest.raises(PromptError, match="other-doc.yaml"):
        prompts.load_prompts(default, custom)


def test_wrong_typed_custom_leaf_says_expected_string(dirs, monkeypatch):
    default, custom = dirs
    _registry(monkeypatch, extra_yaml=("other-doc.yaml",))
    (default / "other-doc.yaml").write_text("bash:\n  description: run bash\n")
    (custom / "other-doc.yaml").write_text("bash:\n  description: 7\n")
    with pytest.raises(PromptError, match="expected a string") as ei:
        prompts.load_prompts(default, custom)
    assert not any("default has no description" in e for e in ei.value.errors)


def test_soul_hierarchy_and_reload_visibility(dirs, tmp_path, monkeypatch):
    default, custom = dirs
    monkeypatch.setattr(config.paths, "data_dir", tmp_path / "data")
    soul_manager._soul_cache.clear()
    prompts.set_current(prompts.load_prompts(default, custom))
    assert soul_manager.get_soul_content("+000123") == "default soul"

    (custom / "soul.md").write_text("custom soul")
    assert soul_manager.get_soul_content("+000123") == "default soul"  # not reloaded yet
    prompts.set_current(prompts.load_prompts(default, custom))
    assert soul_manager.get_soul_content("+000123") == "custom soul"  # visible at once, no cache

    assert soul_manager.write_channel_soul("+000123", "channel soul")
    assert soul_manager.get_soul_content("+000123") == "channel soul"
    soul_manager._soul_cache.clear()


# ---- shipped prompt set (config/prompts/default) ----

import re

from src.nixclaw_agent import llm
from src.nixclaw_agent.types import Tool

_PH = re.compile(r"\{\{(\w+)\}\}")


def test_every_registry_row_loads_from_default_and_placeholders_are_declared():
    ps = prompts.load_prompts(prompts.DEFAULT_DIR, Path("/nonexistent"))
    for spec in prompts.REGISTRY:
        text = ps.text(spec.name)
        assert text, spec.name
        assert set(_PH.findall(text)) <= spec.placeholders, spec.name
        for token in spec.required:
            assert token in text, (spec.name, token)


def test_every_default_md_and_yaml_key_is_registered():
    registered_md = {s.file for s in prompts.REGISTRY if s.file.endswith(".md")}
    assert {p.name for p in prompts.DEFAULT_DIR.glob("*.md")} == registered_md
    import yaml
    for f in {s.file for s in prompts.REGISTRY if s.file.endswith(".yaml")}:
        keys = {s.key for s in prompts.REGISTRY if s.file == f}
        assert set(yaml.safe_load((prompts.DEFAULT_DIR / f).read_text())) == keys, f


def _tools(*names):
    return [Tool(name=n, description=f"desc {n}", parameters={"properties": {"a": {}}}) for n in names]


def _system_prompt(monkeypatch, tier="Privileged", tools=("message_search",), muted=False, channel="+000", debug="off"):
    monkeypatch.setattr(llm, "is_channel_muted", lambda c: muted)
    monkeypatch.setattr(llm, "get_skill_routing_for_channel", lambda c: "ROUTING")
    monkeypatch.setattr(config.debug_logging, "mode", debug)
    monkeypatch.setattr(config.agent, "name", "Nixclaw")
    client = object.__new__(llm.LLMClient)
    return client.build_system_prompt(_tools(*tools), tier, None, None, "+1", channel)


def test_system_prompt_sections_by_tier_mute_debug(monkeypatch):
    p = _system_prompt(monkeypatch)
    assert p.startswith("You are Nixclaw, a helpful AI assistant.")
    assert "★ PERSONAL INFO" in p and "★ MUTE MODE" in p and "★ MULTI-TURN" in p
    assert "★ DEBUG MODE" not in p and "★ UNMUTE MODE" not in p
    assert "- Unknown: Only the get_skill tool" in p

    p = _system_prompt(monkeypatch, tier="Unknown", tools=("get_skill",), muted=True, debug="auto")
    assert "★ INFORMATION LIMITATIONS" in p and "★ PERSONAL INFO" not in p
    assert "★ UNMUTE MODE" in p and "Nixclaw, are you there?" in p
    assert "★ MULTI-TURN" not in p and "★ DEBUG MODE" in p

    p = _system_prompt(monkeypatch, channel=None)
    assert "MUTE MODE" not in p and "★ MULTI-TURN" in p


def test_both_clients_build_identical_system_prompts(monkeypatch):
    monkeypatch.setattr(llm, "is_channel_muted", lambda c: False)
    monkeypatch.setattr(llm, "get_skill_routing_for_channel", lambda c: "ROUTING")
    a = object.__new__(llm.LLMClient).build_system_prompt(_tools("bash"), "Known", "soul", "mem", "+1", "+000")
    b = object.__new__(llm.VLLMClient).build_system_prompt(_tools("bash"), "Known", "soul", "mem", "+1", "+000")
    assert a.split("CURRENT DATETIME")[0] == b.split("CURRENT DATETIME")[0]


def test_custom_override_changes_system_prompt_and_soul_stays_literal(monkeypatch, tmp_path):
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "system-prompt.md").write_text("I am {{agent_name}}.\nSoul: {{soul_section}}\nTZ: {{timezone}}")
    (custom / "system-prompts.yaml").write_text("soul_section: 'SOUL[{{soul_content}}]'\n")
    prompts.set_current(prompts.load_prompts(prompts.DEFAULT_DIR, custom))
    monkeypatch.setattr(llm, "is_channel_muted", lambda c: False)
    monkeypatch.setattr(llm, "get_skill_routing_for_channel", lambda c: "ROUTING")
    monkeypatch.setattr(config.agent, "name", "Zed")
    p = object.__new__(llm.LLMClient).build_system_prompt(_tools("bash"), "Known", "keep {{timezone}}", None, "+1", "+000")
    assert p.startswith("I am Zed.\nSoul: SOUL[keep {{timezone}}]\nTZ: ")


def test_custom_missing_required_marker_is_rejected(tmp_path):
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "mute.md").write_text("Be quiet when asked.")
    with pytest.raises(PromptError, match="__MUTE__"):
        prompts.load_prompts(prompts.DEFAULT_DIR, custom)
