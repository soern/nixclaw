"""Configurable prompt loader for nixclaw-agent.

All model-facing prompt text and canned user-facing replies live in files under
config/prompts/:

    default/   git-tracked, the sole source of truth for the shipped text
    custom/    git-ignored per-instance overrides; every file is optional and a
               missing directory is treated as empty

Two file kinds:
  * .md files hold one prompt each. custom/X.md wins over default/X.md as a
    whole file.
  * .yaml files hold many short strings. They are merged per key at leaf level
    (custom keys override default keys; a key in custom that does not exist in
    default is an error, which catches typos).

Templates may contain {{name}} placeholders. Rendering is single pass over the
template only, so substituted values (soul text, memory, user text) are never
re-scanned. Which placeholders a prompt may use, and which literal tokens it must
still contain (e.g. __MUTE__), are declared in REGISTRY below and validated at
load time. Validation collects ALL problems and raises one PromptError.

How to add a prompt:
  1. Add the default text as config/prompts/default/<file> (for a .md file) or
     as a key in a default .yaml file.
  2. Add ONE PromptSpec row to REGISTRY (name used by code, file, yaml key,
     allowed placeholders, required tokens).
  3. Use `prompts.current().text(name)` or `.render(name, **values)` in code.
A new yaml file that is not referenced by any registry row (e.g.
tool-descriptions.yaml) goes into EXTRA_YAML_FILES and is read with
`prompts.current().doc(filename)`.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

LOG = logging.getLogger(__name__)

PROMPTS_ROOT = Path(__file__).parent.parent.parent / "config" / "prompts"
DEFAULT_DIR = PROMPTS_ROOT / "default"
CUSTOM_DIR = PROMPTS_ROOT / "custom"

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


class PromptError(Exception):
    """Prompt loading/validation failed. `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class PromptSpec:
    """One registered prompt.

    name:         identifier used by code (`current().text(name)`)
    file:         "x.md" (whole file is the prompt) or "x.yaml" (see key)
    key:          top-level yaml key; must be None for .md files
    placeholders: {{names}} the template may contain (omitting them is fine)
    required:     literal tokens that must appear in the effective text
    """

    name: str
    file: str
    key: Optional[str] = None
    placeholders: frozenset[str] = frozenset()
    required: tuple[str, ...] = ()


def _p(*names: str) -> frozenset[str]:
    return frozenset(names)


REGISTRY: tuple[PromptSpec, ...] = (
    PromptSpec("soul", "soul.md"),
    # --- .md prompts ---
    PromptSpec(
        "system-prompt", "system-prompt.md",
        placeholders=_p("agent_name", "tools_description", "user_tier", "user_phone_number",
                        "soul_section", "memory_section", "skill_routing", "current_datetime", "timezone"),
    ),
    PromptSpec("mute", "mute.md", required=("__MUTE__",)),
    PromptSpec("unmute", "unmute.md", placeholders=_p("agent_name"), required=("__UNMUTE__",)),
    PromptSpec("continue", "continue.md", required=("__AGENT_CONTINUE__",)),
    PromptSpec("personal-info", "personal-info.md"),
    PromptSpec("information-limitations", "information-limitations.md"),
    PromptSpec("cron-run-preamble", "cron-run-preamble.md", required=("__DISCARD_OUTPUT__",)),
    PromptSpec("memory-classifier", "memory-classifier.md",
               required=("should_remember", "memory_type", "content")),
    PromptSpec("summarize", "summarize.md", placeholders=_p("max_keywords", "prior_section", "conversation_text")),
    PromptSpec("distill-keywords", "distill-keywords.md", placeholders=_p("max_keywords", "prior_keywords", "summary")),
    PromptSpec("debug-mode", "debug-mode.md", required=("__ISSUE_DETECTED__",)),
    PromptSpec("background-activity", "background-activity.md", placeholders=_p("source_type", "source_id")),
    PromptSpec("single-message-replace", "single-message-replace.md", placeholders=_p("replaced_tag")),
    PromptSpec("voice-call", "voice-call.md"),
    PromptSpec("skill-routing", "skill-routing.md", placeholders=_p("skill_list")),
    PromptSpec("captcha-guidance", "captcha-guidance.md"),
    PromptSpec("markup-corrective", "markup-corrective.md"),
    PromptSpec("markup-reasoning-corrective", "markup-reasoning-corrective.md"),
    # Used by the git pre-commit hook (scripts/precommit_privacy_check.py), not by the agent.
    PromptSpec("precommit-privacy-check", "precommit-privacy-check.md", placeholders=_p("repo_name", "file_list"),
               required=("VERDICT: PASS", "VERDICT: FAIL")),
    # --- system-prompts.yaml (short model-facing strings) ---
    PromptSpec("soul-section", "system-prompts.yaml", "soul_section", _p("soul_content")),
    PromptSpec("memory-section", "system-prompts.yaml", "memory_section", _p("memory_context")),
    PromptSpec("skill-routing-empty", "system-prompts.yaml", "skill_routing_empty"),
    PromptSpec("summarize-prior-block", "system-prompts.yaml", "summarize_prior_block",
               _p("prior_summary", "prior_keywords")),
    PromptSpec("history-keywords", "system-prompts.yaml", "history_keywords", _p("keywords")),
    PromptSpec("conversation-summary", "system-prompts.yaml", "conversation_summary", _p("summary_text")),
    PromptSpec("progress-summary", "system-prompts.yaml", "progress_summary", _p("summary_text")),
    PromptSpec("context-recovery", "system-prompts.yaml", "context_recovery", _p("message")),
    PromptSpec("empty-response-nudge", "system-prompts.yaml", "empty_response_nudge"),
    PromptSpec("tool-failure-nudge", "system-prompts.yaml", "tool_failure_nudge", _p("tool_names", "error")),
    PromptSpec("repeated-tool-call-hint", "system-prompts.yaml", "repeated_tool_call_hint", _p("tool_name", "count")),
    PromptSpec("inbound-drain", "system-prompts.yaml", "inbound_drain", _p("message")),
    PromptSpec("context-exceeded", "system-prompts.yaml", "context_exceeded"),
    PromptSpec("analyze-image", "system-prompts.yaml", "analyze_image", _p("query")),
    PromptSpec("selector-help", "system-prompts.yaml", "selector_help"),
    PromptSpec("no-page-error", "system-prompts.yaml", "no_page_error"),
    PromptSpec("soul-updated", "system-prompts.yaml", "soul_updated", _p("channel_id")),
    PromptSpec("consent-not-run-message", "system-prompts.yaml", "consent_not_run_message", _p("question", "message")),
    PromptSpec("consent-rejected", "system-prompts.yaml", "consent_rejected", _p("voter", "question")),
    PromptSpec("consent-expired", "system-prompts.yaml", "consent_expired", _p("question")),
    PromptSpec("consent-skipped", "system-prompts.yaml", "consent_skipped"),
    PromptSpec("consent-approved-suffix", "system-prompts.yaml", "consent_approved_suffix", _p("voter")),
    PromptSpec("consent-background-pending", "system-prompts.yaml", "consent_background_pending"),
    PromptSpec("consent-inspection", "system-prompts.yaml", "consent_inspection", _p("command", "output")),
    # --- user-messages.yaml (canned replies sent to humans) ---
    PromptSpec("operation-halted", "user-messages.yaml", "operation_halted"),
    PromptSpec("context-reset", "user-messages.yaml", "context_reset"),
    PromptSpec("voice-transcription-failed", "user-messages.yaml", "voice_transcription_failed"),
    PromptSpec("llm-unavailable", "user-messages.yaml", "llm_unavailable"),
    PromptSpec("empty-response", "user-messages.yaml", "empty_response"),
    PromptSpec("too-many-messages", "user-messages.yaml", "too_many_messages"),
    PromptSpec("internal-limit", "user-messages.yaml", "internal_limit"),
    PromptSpec("no-summary-fallback", "user-messages.yaml", "no_summary_fallback"),
    PromptSpec("cron-failed", "user-messages.yaml", "cron_failed", _p("time", "count")),
    PromptSpec("task-completed", "user-messages.yaml", "task_completed"),
    PromptSpec("context-limit-fallback", "user-messages.yaml", "context_limit_fallback"),
    PromptSpec("consent-answer-yes", "user-messages.yaml", "consent_answer_yes"),
    PromptSpec("consent-answer-no", "user-messages.yaml", "consent_answer_no"),
    PromptSpec("consent-expired-restart", "user-messages.yaml", "consent_expired_restart"),
    PromptSpec("web-auth-code", "user-messages.yaml", "web_auth_code", _p("code", "minutes")),
    PromptSpec("web-status-header", "user-messages.yaml", "web_status_header"),
    PromptSpec("web-status-line", "user-messages.yaml", "web_status_line",
               _p("index", "name", "created", "last_used", "expires", "connections")),
    PromptSpec("web-status-footer", "user-messages.yaml", "web_status_footer"),
    PromptSpec("web-status-empty", "user-messages.yaml", "web_status_empty"),
    PromptSpec("web-status-never", "user-messages.yaml", "web_status_never"),
    PromptSpec("web-deauth-done", "user-messages.yaml", "web_deauth_done", _p("name")),
    PromptSpec("web-deauth-not-found", "user-messages.yaml", "web_deauth_not_found", _p("query")),
    PromptSpec("web-deauth-ambiguous", "user-messages.yaml", "web_deauth_ambiguous", _p("query")),
    PromptSpec("web-deauth-usage", "user-messages.yaml", "web_deauth_usage"),
    PromptSpec("web-command-group-hint", "user-messages.yaml", "web_command_group_hint"),
    PromptSpec("web-mirror-direct", "user-messages.yaml", "web_mirror_direct", _p("text")),
    PromptSpec("web-mirror-group", "user-messages.yaml", "web_mirror_group", _p("sender", "text")),
    PromptSpec("voice-call-greeting", "user-messages.yaml", "voice_call_greeting", _p("agent_name")),
    PromptSpec("voice-mirror", "user-messages.yaml", "voice_mirror", _p("text")),
)

# yaml files read as whole documents (not through registry rows).
EXTRA_YAML_FILES: tuple[str, ...] = ("tool-descriptions.yaml",)


def _yaml_files() -> list[str]:
    files = {s.file for s in REGISTRY if s.file.endswith(".yaml")}
    files.update(EXTRA_YAML_FILES)
    return sorted(files)


def _md_files() -> list[str]:
    return sorted(s.file for s in REGISTRY if s.file.endswith(".md"))


class PromptSet:
    """Immutable set of effective prompt texts."""

    def __init__(self, texts: Mapping[str, str], docs: Mapping[str, dict]) -> None:
        self._texts = dict(texts)
        self._docs = dict(docs)
        self._specs = {s.name: s for s in REGISTRY}

    def text(self, name: str) -> str:
        """Effective text of a registered prompt (placeholders left as-is)."""
        return self._texts[name]

    def doc(self, filename: str) -> dict:
        """Merged yaml document for an EXTRA_YAML_FILES entry (do not mutate)."""
        return self._docs[filename]

    def render(self, name: str, /, **values: Any) -> str:
        """Single-pass {{placeholder}} substitution over the template only."""
        allowed = self._specs[name].placeholders
        extra = set(values) - allowed
        if extra:
            raise PromptError([f"{name}: render() got undeclared placeholder values {sorted(extra)}"])

        def lookup(m: "re.Match[str]") -> str:
            key = m.group(1)
            if key not in values:
                raise PromptError([f"{name}: no value passed for placeholder {{{{{key}}}}}"])
            return str(values[key])

        return _PLACEHOLDER_RE.sub(lookup, self._texts[name])


def _read_md(name: str, default_dir: Path, custom_dir: Path, errors: list[str]) -> Optional[str]:
    custom = custom_dir / name
    if custom.is_file():
        path = custom
    else:
        path = default_dir / name
        if not path.is_file():
            errors.append(f"missing default prompt file: {path}")
            return None
    LOG.debug("Loaded prompt %s from %s", name, path)
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        errors.append(f"cannot read {path}: {e}")
        return None


def _read_yaml(path: Path, errors: list[str]) -> Optional[dict]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        errors.append(f"cannot parse {path}: {e}")
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path}: top level must be a mapping")
        return None
    return data


def _merge(default: dict, custom: dict, where: str, errors: list[str]) -> dict:
    """Leaf-level merge of custom over default; unknown custom keys are errors."""
    merged = dict(default)
    for key, value in custom.items():
        if key not in default:
            errors.append(f"{where}: unknown key {key!r} (not in default)")
        elif isinstance(default[key], dict):
            if isinstance(value, dict):
                merged[key] = _merge(default[key], value, f"{where}.{key}", errors)
            else:
                errors.append(f"{where}.{key}: must be a mapping")
        elif not isinstance(value, str):
            errors.append(f"{where}.{key}: expected a string, got {type(value).__name__}")
        else:
            merged[key] = value
    return merged


def _load_yaml_doc(name: str, default_dir: Path, custom_dir: Path, errors: list[str]) -> dict:
    default_path = default_dir / name
    if not default_path.is_file():
        errors.append(f"missing default prompt file: {default_path}")
        return {}
    default = _read_yaml(default_path, errors)
    if default is None:
        return {}
    custom_path = custom_dir / name
    if custom_path.is_file():
        custom = _read_yaml(custom_path, errors)
        if custom is not None:
            LOG.debug("Loaded prompt overrides for %s from %s", name, custom_path)
            return _merge(default, custom, name, errors)
    return default


def _validate_text(spec: PromptSpec, text: str, errors: list[str]) -> None:
    for ph in sorted(set(_PLACEHOLDER_RE.findall(text)) - spec.placeholders):
        errors.append(f"{spec.name}: unknown placeholder {{{{{ph}}}}} (allowed: {sorted(spec.placeholders)})")
    for token in spec.required:
        if token not in text:
            errors.append(f"{spec.name}: required token {token!r} is missing")


def _check_tool_descriptions(doc: dict, errors: list[str]) -> None:
    """Every tool (and every param) in the code schemas needs an entry, and vice versa.

    Runs on the merged doc; custom files cannot add keys, so it has the default's key set.

    Imported lazily: the tools package imports this module.
    """
    from .tools import TOOL_DEFINITIONS

    for tool, definition in TOOL_DEFINITIONS.items():
        entry = doc.get(tool)
        if not isinstance(entry, dict) or not isinstance(entry.get("description"), str):
            errors.append(f"tool-descriptions.yaml: default has no description for tool {tool!r}")
            continue
        params = entry.get("params")
        params = params if isinstance(params, dict) else {}
        for pname in definition["params"].get("properties", {}):
            if pname not in params:
                errors.append(f"tool-descriptions.yaml: default has no entry for {tool}.{pname}")
        for pname in params:
            if pname not in definition["params"].get("properties", {}):
                errors.append(f"tool-descriptions.yaml: {tool}.{pname} is not a parameter of that tool")
    for tool in doc:
        if tool not in TOOL_DEFINITIONS:
            errors.append(f"tool-descriptions.yaml: {tool!r} is not a known tool")


def load_prompts(default_dir: Optional[Path] = None, custom_dir: Optional[Path] = None) -> PromptSet:
    """Load and validate all registered prompts.

    Raises PromptError listing every problem; never returns a partial set.
    """
    default_dir = DEFAULT_DIR if default_dir is None else default_dir
    custom_dir = CUSTOM_DIR if custom_dir is None else custom_dir
    errors: list[str] = []

    docs = {name: _load_yaml_doc(name, default_dir, custom_dir, errors) for name in _yaml_files()}
    if docs.get("tool-descriptions.yaml"):
        _check_tool_descriptions(docs["tool-descriptions.yaml"], errors)

    # A misnamed custom file would otherwise be silently ignored. Custom-dir
    # handling stays quiet (DEBUG only): a stray file must never stop nixclaw.
    if custom_dir.is_dir():
        known = set(_md_files()) | set(_yaml_files())
        for p in sorted(custom_dir.iterdir()):
            if p.is_file() and not p.name.startswith(".") and p.name not in known:
                LOG.debug("Ignoring unknown custom prompt file: %s (no such prompt)", p)

    texts: dict[str, str] = {}
    for spec in REGISTRY:
        text: Any
        if spec.file.endswith(".md"):
            text = _read_md(spec.file, default_dir, custom_dir, errors)
        else:
            text = docs[spec.file].get(spec.key)
            if text is None:
                if (default_dir / spec.file).is_file():
                    errors.append(f"{spec.file}: missing key {spec.key!r}")
            elif not isinstance(text, str):
                errors.append(f"{spec.file}: key {spec.key!r} must be a string")
                text = None
        if text is None:
            continue
        text = text.strip()
        _validate_text(spec, text, errors)
        texts[spec.name] = text

    if errors:
        raise PromptError(errors)
    return PromptSet(texts, docs)


def load_prompt(name: str, default_dir: Optional[Path] = None, custom_dir: Optional[Path] = None) -> PromptSet:
    """Load and validate ONE registered .md prompt (same default/custom rules).

    For tools that run outside the agent (the git pre-commit hook): unlike
    load_prompts() it does not import the tools package, so it needs only pyyaml.
    """
    default_dir = DEFAULT_DIR if default_dir is None else default_dir
    custom_dir = CUSTOM_DIR if custom_dir is None else custom_dir
    spec = next((s for s in REGISTRY if s.name == name), None)
    if spec is None or not spec.file.endswith(".md"):
        raise PromptError([f"{name}: not a registered .md prompt"])
    errors: list[str] = []
    text = _read_md(spec.file, default_dir, custom_dir, errors)
    if text is not None:
        _validate_text(spec, text, errors)
    if errors or text is None:
        raise PromptError(errors)
    return PromptSet({name: text}, {})


_current: Optional[PromptSet] = None


def current() -> PromptSet:
    """The active PromptSet; loads default/ + custom/ on first access."""
    global _current
    if _current is None:
        _current = load_prompts()
    return _current


def set_current(prompt_set: PromptSet) -> None:
    """Swap the active PromptSet (startup and reload)."""
    global _current
    _current = prompt_set
