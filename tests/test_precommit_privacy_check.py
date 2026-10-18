"""Tests for scripts/precommit_privacy_check.py (git pre-commit LLM privacy check).

pi is replaced by a stub shell script (command.executable), so no LLM is needed.
"""

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "precommit_privacy_check", REPO_ROOT / "scripts" / "precommit_privacy_check.py")
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
sys.modules["precommit_privacy_check"] = hook
_spec.loader.exec_module(hook)


def _raw(**overrides):
    section = {
        "enabled": True,
        "on_error": "block",
        "timeout_seconds": 20,
        "max_diff_bytes": 100_000,
        "llm": {"api": "openai-completions", "base_url": "http://llm.invalid:8000/v1", "model": "m1",
                "api_key_env": "", "thinking": "low"},
        "command": {"runner": "pi", "executable": "pi", "extra_args": []},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(section.get(key), dict):
            section[key] = {**section[key], **value}
        else:
            section[key] = value
    return {"precommit_privacy_check": section}


# --- config -----------------------------------------------------------------

def test_example_config_is_valid():
    raw = yaml.safe_load((REPO_ROOT / "config" / "dev.yaml.example").read_text())
    cfg = hook.parse_config(raw)
    assert cfg.runner == "pi" and cfg.api in hook.API_TYPES and cfg.on_error == "block"


def test_config_rejects_bad_values_and_lists_all():
    with pytest.raises(hook.ConfigError) as ei:
        hook.parse_config(_raw(llm={"api": "openai-decisions", "model": ""}, command={"runner": "aider"}))
    msg = str(ei.value)
    assert "llm.api" in msg and "llm.model" in msg and "command.runner" in msg


def test_config_error_keeps_valid_on_error():
    with pytest.raises(hook.ConfigError) as ei:
        hook.parse_config(_raw(on_error="warn", timeout_seconds="soon"))
    assert ei.value.on_error == "warn"


def test_config_bad_on_error_falls_back_to_block():
    with pytest.raises(hook.ConfigError) as ei:
        hook.parse_config(_raw(on_error="ignore"))
    assert ei.value.on_error == "block"


def test_missing_config_file(tmp_path):
    with pytest.raises(hook.ConfigError, match="not found"):
        hook.load_config(tmp_path / "dev.yaml")


def test_api_key_from_env(monkeypatch):
    cfg = hook.parse_config(_raw(llm={"api_key_env": "TEST_PRECOMMIT_KEY"}))
    monkeypatch.delenv("TEST_PRECOMMIT_KEY", raising=False)
    with pytest.raises(hook.HookError, match="TEST_PRECOMMIT_KEY"):
        hook.pi_models_json(cfg)
    monkeypatch.setenv("TEST_PRECOMMIT_KEY", "k-123")
    provider = hook.pi_models_json(cfg)["providers"][hook.PI_PROVIDER]
    assert provider["apiKey"] == "k-123"


def test_models_json_shape_and_overrides():
    cfg = hook.parse_config(_raw(llm={"api": "openai-responses", "thinking": "off",
                                      "pi_model_options": {"compat": {"x": 1}}}))
    provider = hook.pi_models_json(cfg)["providers"][hook.PI_PROVIDER]
    assert provider["api"] == "openai-responses"
    assert provider["baseUrl"] == "http://llm.invalid:8000/v1"
    model = provider["models"][0]
    assert model["id"] == "m1" and model["reasoning"] is False and model["compat"] == {"x": 1}


# --- verdict / forbidden paths ----------------------------------------------

@pytest.mark.parametrize("output,expected", [
    ("FINDINGS:\n- none\nVERDICT: PASS\n", "PASS"),
    ("**VERDICT: FAIL**", "FAIL"),
    ("VERDICT: **PASS**", "PASS"),
    ("I will answer VERDICT: PASS unless...\nFINDINGS:\n- a.py:1 | secret | key\nVERDICT: FAIL", "FAIL"),
])
def test_parse_verdict_uses_last(output, expected):
    assert hook.parse_verdict(output) == expected


def test_parse_verdict_missing():
    with pytest.raises(hook.HookError):
        hook.parse_verdict("looks fine to me")


def test_forbidden_paths():
    paths = ["config/agent.yaml", "config/dev.yaml", ".env", "deploy/x/.env", "deploy/x/.env.example",
             "store/messages.db", "data/+000000/a.txt", "tests/fixtures/data/ok.txt", "x.db-wal",
             "config/prompts/custom/soul.md", "config/prompts/custom/.gitkeep", "src/a.py",
             "config/agent.yaml.example", "config/dev.yaml.example",
             "config/webinterface.yaml", "config/webinterface.yaml.example", "config/ssl/key.pem"]
    assert hook.forbidden(paths) == ["config/agent.yaml", "config/dev.yaml", ".env", "deploy/x/.env",
                                     "store/messages.db", "data/+000000/a.txt", "x.db-wal",
                                     "config/prompts/custom/soul.md", "config/webinterface.yaml",
                                     "config/ssl/key.pem"]


def test_shipped_prompt_loads_standalone():
    text = hook.prompts.load_prompt(hook.PROMPT_NAME).render(hook.PROMPT_NAME, repo_name="r", file_list="A x.py")
    assert "VERDICT: PASS" in text and "VERDICT: FAIL" in text and "A x.py" in text


# --- end to end with a temp repo and a stub pi --------------------------------

@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


def _stage(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)


def _stub(tmp_path, body):
    """Fake pi: records args + env-provided models.json, then runs body."""
    stub = tmp_path / "fake-pi"
    record = tmp_path / "record"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\0" "$@" > "{record}.args"\n'
        f'cp "$PI_CODING_AGENT_DIR/models.json" "{record}.models.json"\n'
        f"{body}\n")
    stub.chmod(0o755)
    return stub, record


def _write_cfg(tmp_path, stub, **overrides):
    raw = _raw(command={"executable": str(stub)}, **overrides)
    path = tmp_path / "dev.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_pass(repo, tmp_path, capsys):
    stub, record = _stub(tmp_path, 'echo "FINDINGS:"; echo "- none"; echo "VERDICT: PASS"')
    _stage(repo, "a.py", "x = 1\n")
    assert hook.run(repo, _write_cfg(tmp_path, stub)) == 0
    args = Path(f"{record}.args").read_text().split("\0")[:-1]
    assert "--no-tools" in args and "--no-extensions" in args and "--no-context-files" in args
    assert args[args.index("--model") + 1] == f"{hook.PI_PROVIDER}/m1"
    diff_arg = args[args.index("--") + 1]
    assert diff_arg.startswith("@") and diff_arg.endswith("staged.diff")
    assert "A a.py" in args[-1]
    models = json.loads(Path(f"{record}.models.json").read_text())
    assert models["providers"][hook.PI_PROVIDER]["models"][0]["id"] == "m1"
    assert "PASS" in capsys.readouterr().err


def test_fail_blocks_even_with_warn(repo, tmp_path, capsys):
    stub, _ = _stub(tmp_path, 'echo "- a.py:1 | secret | api key sk-l..."; echo "VERDICT: FAIL"')
    _stage(repo, "a.py", 'KEY = "sk-live-0123456789"\n')
    assert hook.run(repo, _write_cfg(tmp_path, stub, on_error="warn")) == 1
    err = capsys.readouterr().err
    assert "api key" in err and "FAIL" in err


@pytest.mark.parametrize("on_error,expected", [("block", 1), ("warn", 0)])
def test_exec_failure_follows_on_error(repo, tmp_path, on_error, expected):
    stub, _ = _stub(tmp_path, 'echo "boom" >&2; exit 3')
    _stage(repo, "a.py", "x = 1\n")
    assert hook.run(repo, _write_cfg(tmp_path, stub, on_error=on_error)) == expected


def test_no_verdict_is_exec_failure(repo, tmp_path):
    stub, _ = _stub(tmp_path, 'echo "looks fine"')
    _stage(repo, "a.py", "x = 1\n")
    assert hook.run(repo, _write_cfg(tmp_path, stub)) == 1
    assert hook.run(repo, _write_cfg(tmp_path, stub, on_error="warn")) == 0


def test_timeout_kills_process_group(repo, tmp_path):
    marker = tmp_path / "child-survived"
    stub, _ = _stub(tmp_path, f'(sleep 3; touch "{marker}") & sleep 30')
    _stage(repo, "a.py", "x = 1\n")
    start = time.monotonic()
    assert hook.run(repo, _write_cfg(tmp_path, stub, timeout_seconds=1)) == 1
    assert time.monotonic() - start < 10
    time.sleep(3.5)
    assert not marker.exists()


def test_missing_executable(repo, tmp_path):
    _stage(repo, "a.py", "x = 1\n")
    assert hook.run(repo, _write_cfg(tmp_path, tmp_path / "no-such-pi", on_error="warn")) == 0
    assert hook.run(repo, _write_cfg(tmp_path, tmp_path / "no-such-pi")) == 1


def test_diff_too_large(repo, tmp_path):
    stub, record = _stub(tmp_path, 'echo "VERDICT: PASS"')
    _stage(repo, "big.txt", "x" * 5000 + "\n")
    assert hook.run(repo, _write_cfg(tmp_path, stub, max_diff_bytes=1000)) == 1
    assert not Path(f"{record}.args").exists()


def test_missing_config_blocks(repo, tmp_path):
    _stage(repo, "a.py", "x = 1\n")
    assert hook.run(repo, tmp_path / "dev.yaml") == 1


def test_nothing_staged_and_deletions_only(repo, tmp_path):
    stub, record = _stub(tmp_path, 'echo "VERDICT: FAIL"')
    cfg = _write_cfg(tmp_path, stub)
    assert hook.run(repo, cfg) == 0
    _stage(repo, "a.py", "x = 1\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "i",
                    "--no-verify"], cwd=repo, check=True)
    subprocess.run(["git", "rm", "-q", "a.py"], cwd=repo, check=True)
    assert hook.run(repo, cfg) == 0
    assert not Path(f"{record}.args").exists()


def test_forbidden_blocks_without_llm_even_when_skipped(repo, tmp_path, monkeypatch):
    stub, record = _stub(tmp_path, 'echo "VERDICT: PASS"')
    _stage(repo, "config/agent.yaml", "auth: {}\n")
    monkeypatch.setenv(hook.SKIP_ENV, "1")
    assert hook.run(repo, _write_cfg(tmp_path, stub)) == 1
    assert not Path(f"{record}.args").exists()


def test_skip_env_and_disabled(repo, tmp_path, monkeypatch):
    stub, record = _stub(tmp_path, 'echo "VERDICT: FAIL"')
    _stage(repo, "a.py", "x = 1\n")
    assert hook.run(repo, _write_cfg(tmp_path, stub, enabled=False)) == 0
    monkeypatch.setenv(hook.SKIP_ENV, "1")
    assert hook.run(repo, _write_cfg(tmp_path, stub)) == 0
    assert not Path(f"{record}.args").exists()


def test_renamed_file_listed_by_new_path(repo, tmp_path):
    _stage(repo, "old.py", "x = 1\n" * 20)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "i",
                    "--no-verify"], cwd=repo, check=True)
    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=repo, check=True)
    assert hook.staged_files(repo) == [("R", "new.py")]
