#!/usr/bin/env python3
"""Git pre-commit hook: let an LLM check the staged changes for secrets and personal data.

Called by scripts/git-hooks/pre-commit (install: scripts/install-git-hooks.sh).
Config: precommit_privacy_check in config/dev.yaml (template: config/dev.yaml.example).
Prompt: registry entry "precommit-privacy-check" (config/prompts/{default,custom}/).

Steps:
  1. Block staged paths that must never be committed (no LLM, always runs).
  2. Unless skipped (SKIP_PRIVACY_CHECK=1 / enabled: false): write the staged diff
     to a temp file and run the pi coding agent with tools, extensions, skills and
     context files disabled, against a throwaway PI_CODING_AGENT_DIR whose
     models.json holds only the configured provider. The diff is untrusted, so
     the agent gets no tools a prompt injection could abuse.
  3. The last "VERDICT: PASS|FAIL" in the output decides. FAIL always blocks;
     any failure to get a verdict follows on_error (block | warn).

Needs only python3 + pyyaml (no agent dependencies).
"""

import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.nixclaw_agent import prompts  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config" / "dev.yaml"
SKIP_ENV = "SKIP_PRIVACY_CHECK"
PROMPT_NAME = "precommit-privacy-check"
PI_PROVIDER = "nixclaw-precommit"

API_TYPES = ("openai-completions", "openai-responses")
ON_ERROR = ("block", "warn")
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
RUNNERS = ("pi",)

# CLAUDE.md safety rule 2. Matched against repo-relative paths of added/changed files.
FORBIDDEN_PATHS = (
    "config/agent.yaml",
    "config/dev.yaml",
    "config/webinterface.yaml",
    "config/ssl/*",
    ".env",
    "*/.env",
    ".signal-data/*",
    ".signal-cli-data/*",
    "store/*",
    "data/*",
    "groups/*",
    "log-archive/*",
    "issues/*",
    "*.db-shm",
    "*.db-wal",
    "config/prompts/custom/*",
)
FORBIDDEN_EXCEPTIONS = ("config/prompts/custom/.gitkeep",)

_VERDICT_RE = re.compile(r"VERDICT:\s*\**\s*(PASS|FAIL)\b")


class HookError(Exception):
    """The check could not produce a verdict (handled per on_error)."""


class ConfigError(HookError):
    """Invalid config; carries on_error if that key itself was valid."""

    def __init__(self, message: str, on_error: str = "block") -> None:
        super().__init__(message)
        self.on_error = on_error


@dataclass
class HookConfig:
    enabled: bool = True
    on_error: str = "block"
    timeout_seconds: float = 180
    max_diff_bytes: int = 400_000
    api: str = "openai-completions"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    context_window: int = 131072
    max_tokens: int = 16384
    thinking: str = "low"
    pi_model_options: dict = field(default_factory=dict)
    runner: str = "pi"
    executable: str = "pi"
    extra_args: list = field(default_factory=list)


def _section(raw: dict, key: str, where: str, errors: list[str]) -> dict:
    value = raw.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{where}.{key} must be a mapping")
        return {}
    return value


def parse_config(raw: Any) -> HookConfig:
    """Validate the precommit_privacy_check section; HookError lists every problem.

    on_error is validated first and carried by the ConfigError, so a broken rest of
    the config still honours it.
    """
    if not isinstance(raw, dict):
        raise ConfigError("config/dev.yaml: top level must be a mapping")
    section = raw.get("precommit_privacy_check")
    if not isinstance(section, dict):
        raise ConfigError("config/dev.yaml: missing section precommit_privacy_check")

    errors: list[str] = []
    where = "precommit_privacy_check"
    cfg = HookConfig()

    def get(d: dict, key: str, types: tuple, default: Any, sub: str = "") -> Any:
        value = d.get(key, default)
        if isinstance(value, bool) and bool not in types:
            errors.append(f"{where}{sub}.{key}: expected {types[0].__name__}, got bool")
            return default
        if not isinstance(value, types):
            errors.append(f"{where}{sub}.{key}: expected {types[0].__name__}, got {type(value).__name__}")
            return default
        return value

    def choice(value: str, allowed: tuple, name: str) -> None:
        if value not in allowed:
            errors.append(f"{where}.{name}: {value!r} is not one of {', '.join(allowed)}")

    cfg.on_error = get(section, "on_error", (str,), cfg.on_error)
    choice(cfg.on_error, ON_ERROR, "on_error")
    if cfg.on_error not in ON_ERROR:
        cfg.on_error = "block"
    cfg.enabled = get(section, "enabled", (bool,), cfg.enabled)
    cfg.timeout_seconds = get(section, "timeout_seconds", (int, float), cfg.timeout_seconds)
    if cfg.timeout_seconds <= 0:
        errors.append(f"{where}.timeout_seconds must be > 0")
    cfg.max_diff_bytes = get(section, "max_diff_bytes", (int,), cfg.max_diff_bytes)
    if cfg.max_diff_bytes <= 0:
        errors.append(f"{where}.max_diff_bytes must be > 0")

    llm = _section(section, "llm", where, errors)
    cfg.api = get(llm, "api", (str,), cfg.api, ".llm")
    choice(cfg.api, API_TYPES, "llm.api")
    cfg.base_url = get(llm, "base_url", (str,), "", ".llm")
    if not cfg.base_url.startswith(("http://", "https://")):
        errors.append(f"{where}.llm.base_url must be an http(s) URL")
    cfg.model = get(llm, "model", (str,), "", ".llm")
    if not cfg.model:
        errors.append(f"{where}.llm.model is required")
    cfg.api_key_env = get(llm, "api_key_env", (str,), "", ".llm") or ""
    cfg.context_window = get(llm, "context_window", (int,), cfg.context_window, ".llm")
    cfg.max_tokens = get(llm, "max_tokens", (int,), cfg.max_tokens, ".llm")
    cfg.thinking = get(llm, "thinking", (str,), cfg.thinking, ".llm")
    choice(cfg.thinking, THINKING_LEVELS, "llm.thinking")
    cfg.pi_model_options = get(llm, "pi_model_options", (dict,), {}, ".llm") or {}

    command = _section(section, "command", where, errors)
    cfg.runner = get(command, "runner", (str,), cfg.runner, ".command")
    choice(cfg.runner, RUNNERS, "command.runner")
    cfg.executable = get(command, "executable", (str,), cfg.executable, ".command")
    if not cfg.executable:
        errors.append(f"{where}.command.executable is required")
    cfg.extra_args = get(command, "extra_args", (list,), [], ".command") or []
    if not all(isinstance(a, str) for a in cfg.extra_args):
        errors.append(f"{where}.command.extra_args must be a list of strings")

    if errors:
        raise ConfigError("invalid config/dev.yaml: " + "; ".join(errors), cfg.on_error)
    return cfg


def load_config(path: Path) -> HookConfig:
    if not path.is_file():
        raise ConfigError(f"{path} not found (copy config/dev.yaml.example)")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    return parse_config(raw)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise HookError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def staged_files(repo: Path) -> list[tuple[str, str]]:
    """(status, path) of staged added/copied/modified/renamed/type-changed files."""
    out = _git(repo, "diff", "--cached", "--name-status", "-z", "-M", "--diff-filter=ACMRT")
    parts = out.split("\0")
    files: list[tuple[str, str]] = []
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status[0] in "RC":  # old path, new path
            files.append((status[0], parts[i + 2]))
            i += 3
        else:
            files.append((status, parts[i + 1]))
            i += 2
    return files


def staged_diff(repo: Path) -> str:
    return _git(repo, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv",
                "-M", "-U3", "--diff-filter=ACMRT")


def forbidden(paths: list[str]) -> list[str]:
    hits = []
    for p in paths:
        if p in FORBIDDEN_EXCEPTIONS:
            continue
        if any(fnmatch.fnmatchcase(p, pat) for pat in FORBIDDEN_PATHS):
            hits.append(p)
    return hits


def parse_verdict(output: str) -> str:
    """'PASS' or 'FAIL' from the LAST verdict line; HookError if there is none."""
    found = _VERDICT_RE.findall(output)
    if not found:
        raise HookError("no VERDICT line in the LLM output")
    return found[-1]


def pi_models_json(cfg: HookConfig) -> dict:
    api_key = "none"
    if cfg.api_key_env:
        api_key = os.environ.get(cfg.api_key_env, "")
        if not api_key:
            raise HookError(f"env var {cfg.api_key_env} (llm.api_key_env) is not set")
    model = {
        "id": cfg.model,
        "name": cfg.model,
        "contextWindow": cfg.context_window,
        "maxTokens": cfg.max_tokens,
        "reasoning": cfg.thinking != "off",
        "input": ["text"],
    }
    model.update(cfg.pi_model_options)
    return {"providers": {PI_PROVIDER: {
        "baseUrl": cfg.base_url, "api": cfg.api, "apiKey": api_key, "models": [model],
    }}}


def pi_command(cfg: HookConfig, executable: str, diff_path: Path, prompt: str) -> list[str]:
    return [
        executable, "-p", "--no-session",
        "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
        "--no-context-files", "--no-mcp", "--offline", "--no-approve",
        "--model", f"{PI_PROVIDER}/{cfg.model}", "--thinking", cfg.thinking,
        *cfg.extra_args,
        "--", f"@{diff_path}", prompt,
    ]


def run_pi(cfg: HookConfig, diff: str, prompt: str) -> str:
    """Run pi on the diff and return its stdout; HookError on any failure."""
    executable = shutil.which(cfg.executable)
    if executable is None:
        raise HookError(f"command not found: {cfg.executable}")
    with tempfile.TemporaryDirectory(prefix="nixclaw-precommit-") as tmp:
        tmp_path = Path(tmp)
        os.chmod(tmp_path, 0o700)
        agent_dir = tmp_path / "pi-agent"
        agent_dir.mkdir()
        models = agent_dir / "models.json"
        models.write_text(json.dumps(pi_models_json(cfg), indent=2), encoding="utf-8")
        os.chmod(models, 0o600)
        diff_path = tmp_path / "staged.diff"
        diff_path.write_text(diff, encoding="utf-8")

        env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent_dir), PI_OFFLINE="1")
        proc = subprocess.Popen(
            pi_command(cfg, executable, diff_path, prompt), cwd=tmp_path, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=cfg.timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise HookError(f"{cfg.runner} timed out after {cfg.timeout_seconds:g}s")
    if proc.returncode != 0:
        tail = (stderr.strip() or stdout.strip())[-1500:]
        raise HookError(f"{cfg.runner} exited with code {proc.returncode}: {tail}")
    return stdout


def _say(msg: str) -> None:
    print(f"privacy-check: {msg}", file=sys.stderr, flush=True)


def _llm_check(repo: Path, cfg: HookConfig, files: list[tuple[str, str]]) -> int:
    diff = staged_diff(repo)
    if not diff.strip():
        return 0
    size = len(diff.encode("utf-8"))
    if size > cfg.max_diff_bytes:
        raise HookError(f"staged diff is {size} bytes, more than max_diff_bytes={cfg.max_diff_bytes}")
    try:
        prompt = prompts.load_prompt(PROMPT_NAME).render(
            PROMPT_NAME,
            repo_name=repo.name,
            file_list="\n".join(f"{status} {path}" for status, path in files),
        )
    except prompts.PromptError as e:
        raise HookError(f"prompt {PROMPT_NAME}: {e}") from e
    _say(f"asking {cfg.model} at {cfg.base_url} about {len(files)} file(s), {size} bytes "
         f"(timeout {cfg.timeout_seconds:g}s) ...")
    output = run_pi(cfg, diff, prompt)
    verdict = parse_verdict(output)
    if verdict == "PASS":
        _say("PASS")
        return 0
    print(output.strip(), file=sys.stderr)
    _say("FAIL - commit blocked. Fix the findings above, or if they are false alarms, "
         f"commit with {SKIP_ENV}=1 (or --no-verify).")
    return 1


def run(repo: Path, config_path: Path) -> int:
    """Hook entry point; returns the exit code (0 = commit may proceed)."""
    on_error = "block"
    try:
        files = staged_files(repo)
        if not files:
            return 0
        hits = forbidden([p for _, p in files])
        if hits:
            _say("these files must never be committed:\n  " + "\n  ".join(hits)
                 + "\nUnstage them with: git restore --staged <file>")
            return 1
        if os.environ.get(SKIP_ENV) == "1":
            _say(f"LLM check skipped ({SKIP_ENV}=1)")
            return 0
        try:
            cfg = load_config(config_path)
        except ConfigError as e:
            on_error = e.on_error
            raise
        on_error = cfg.on_error
        if not cfg.enabled:
            return 0
        return _llm_check(repo, cfg, files)
    except HookError as e:
        if on_error == "warn":
            _say(f"WARNING: check could not run, committing anyway: {e}")
            return 0
        _say(f"ERROR: check could not run, commit blocked: {e}\n"
             f"Skip the LLM check with {SKIP_ENV}=1 (or the whole hook with --no-verify).")
        return 1


def main() -> int:
    return run(REPO_ROOT, CONFIG_PATH)


if __name__ == "__main__":
    sys.exit(main())
