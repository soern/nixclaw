"""Tool descriptions live in config/prompts/default/tool-descriptions.yaml (overlaid by ToolRegistry)."""

import copy

import pytest
import yaml

from src.nixclaw_agent import prompts
from src.nixclaw_agent.auth import AuthConfig, AuthManager, UserTier
from src.nixclaw_agent.prompts import PromptError
from src.nixclaw_agent.tools import TOOL_DEFINITIONS, register_all_tools
from src.nixclaw_agent.tools.registry import ToolRegistry

DOC = "tool-descriptions.yaml"


@pytest.fixture
def registry():
    reg = ToolRegistry(AuthManager(AuthConfig(privileged_numbers=["+000000"], known_numbers=["+000001"])))
    register_all_tools(reg)
    return reg


@pytest.fixture
def default_doc():
    return yaml.safe_load((prompts.DEFAULT_DIR / DOC).read_text(encoding="utf-8"))


@pytest.fixture
def custom(tmp_path):
    d = tmp_path / "custom"
    d.mkdir()
    return d


def _tool(reg, tier, name):
    return next(t for t in reg.get_tools_for_tier(tier) if t.name == name)


def test_every_tool_and_param_has_default_entry(default_doc):
    assert set(default_doc) == set(TOOL_DEFINITIONS)
    for name, definition in TOOL_DEFINITIONS.items():
        assert isinstance(default_doc[name]["description"], str)
        assert set(default_doc[name]["params"]) == set(definition["params"].get("properties", {}))


def test_definitions_hold_no_description_text():
    def walk(x):
        if isinstance(x, dict):
            assert "description" not in x or isinstance(x["description"], dict)
            for v in x.values():
                walk(v)
    walk(TOOL_DEFINITIONS)


@pytest.mark.parametrize("tier", list(UserTier))
def test_tools_equal_schema_plus_nonempty_descriptions(registry, default_doc, tier):
    tools = registry.get_tools_for_tier(tier)
    assert tools
    for t in tools:
        expected = copy.deepcopy(TOOL_DEFINITIONS[t.name]["params"])
        for pname, pdesc in default_doc[t.name]["params"].items():
            if pdesc:
                expected["properties"][pname]["description"] = pdesc
        assert t.parameters == expected
        assert t.description == default_doc[t.name]["description"]


def test_get_tools_does_not_mutate_definitions(registry):
    before = copy.deepcopy(TOOL_DEFINITIONS)
    registry.get_tools_for_tier(UserTier.PRIVILEGED)
    assert TOOL_DEFINITIONS == before


def test_custom_leaf_override_keeps_siblings(registry, default_doc, custom):
    (custom / DOC).write_text("message_search:\n  params:\n    limit: my limit text\n")
    prompts.set_current(prompts.load_prompts(prompts.DEFAULT_DIR, custom))
    t = _tool(registry, UserTier.KNOWN, "message_search")
    props = t.parameters["properties"]
    assert props["limit"]["description"] == "my limit text"
    assert props["query"]["description"] == default_doc["message_search"]["params"]["query"]
    assert t.description == default_doc["message_search"]["description"]


def test_custom_can_add_description_to_undescribed_param(registry, custom):
    assert "description" not in _tool(registry, UserTier.KNOWN, "file_read").parameters["properties"]["path"]
    (custom / DOC).write_text("file_read:\n  params:\n    path: the file path\n")
    prompts.set_current(prompts.load_prompts(prompts.DEFAULT_DIR, custom))
    assert _tool(registry, UserTier.KNOWN, "file_read").parameters["properties"]["path"]["description"] == "the file path"


def test_set_current_takes_effect_on_next_call(registry, custom):
    old = _tool(registry, UserTier.KNOWN, "file_write").description
    (custom / DOC).write_text("file_write:\n  description: brand new text\n")
    prompts.set_current(prompts.load_prompts(prompts.DEFAULT_DIR, custom))
    assert _tool(registry, UserTier.KNOWN, "file_write").description == "brand new text"
    assert old != "brand new text"


@pytest.mark.parametrize("text, needle", [
    ("nosuchtool:\n  description: x\n", "nosuchtool"),
    ("bash:\n  params:\n    nosuchparam: x\n", "nosuchparam"),
])
def test_unknown_custom_tool_or_param_is_error(custom, text, needle):
    (custom / DOC).write_text(text)
    with pytest.raises(PromptError, match=needle):
        prompts.load_prompts(prompts.DEFAULT_DIR, custom)


def test_missing_default_entry_is_error(tmp_path, default_doc):
    default = tmp_path / "default"
    default.mkdir()
    for f in prompts.DEFAULT_DIR.iterdir():
        (default / f.name).write_bytes(f.read_bytes())
    broken = copy.deepcopy(default_doc)
    del broken["bash"]
    del broken["file_read"]["params"]["path"]
    (default / DOC).write_text(yaml.safe_dump(broken))
    with pytest.raises(PromptError) as ei:
        prompts.load_prompts(default, tmp_path / "nocustom")
    msg = str(ei.value)
    assert "'bash'" in msg and "file_read.path" in msg
