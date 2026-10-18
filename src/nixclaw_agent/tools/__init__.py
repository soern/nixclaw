"""Tools package for nixclaw-agent."""

from typing import Callable

from .registry import ToolRegistry
from ..types import ToolResult
from ..config import config

from . import files
from . import messages
from . import bash
from . import cron
from . import soul
from . import memory
from . import skills
from . import signal_ops
from . import imagegen_ops
from . import tts_ops
from . import stt_ops
from . import channel_mode
from . import message_mode
from . import voice_call
from . import debug_cron
from . import debug_subagent

import importlib.util

try:
    from . import browse
    # browse imports playwright lazily, so also check the package is actually
    # installed - otherwise every tool call would fail at runtime.
    BROWSE_AVAILABLE = importlib.util.find_spec("playwright") is not None
except ImportError:
    BROWSE_AVAILABLE = False


_BROWSER_UNAVAILABLE = "Browser tools not available (playwright not installed)"


def _stub_browser_navigate(url: str = "") -> ToolResult:
    return ToolResult(
        name="browser_navigate",
        success=False,
        output=None,
        error=_BROWSER_UNAVAILABLE,
    )


def _stub_web_search(query: str) -> ToolResult:
    return ToolResult(
        name="web_search",
        success=False,
        output=None,
        error=_BROWSER_UNAVAILABLE,
    )


def _stub_browser_screenshot(url: str = "", filename: str = "") -> ToolResult:
    return ToolResult(
        name="browser_screenshot",
        success=False,
        output=None,
        error=_BROWSER_UNAVAILABLE,
    )


def _stub_browser_interact(action: str, target: str = "", value: str = "") -> ToolResult:
    return ToolResult(
        name="browser_interact",
        success=False,
        output=None,
        error=_BROWSER_UNAVAILABLE,
    )


def _stub_browser_evaluate(script: str) -> ToolResult:
    return ToolResult(
        name="browser_evaluate",
        success=False,
        output=None,
        error=_BROWSER_UNAVAILABLE,
    )


TOOL_DEFINITIONS: dict[str, dict] = {
    "file_read": {
        "params": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    "file_write": {
        "params": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    "file_grep": {
        "params": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern", "path"],
        },
    },
    "file_list": {
        "params": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    "file_search": {
        "params": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern", "path"],
        },
    },
    "message_search": {
        "params": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "approximate_time": {"type": "string"},
                "before": {"type": "string"},
                "after": {"type": "string"},
                "context_lines": {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    "bash": {
        "params": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
            },
            "required": ["cmd"],
        },
    },
    "browser_navigate": {
        "params": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": [],
        },
    },
    "web_search": {
        "params": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    "browser_screenshot": {
        "params": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "filename": {"type": "string"},
            },
            "required": [],
        },
    },
    "browser_interact": {
        "params": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["click", "type", "press", "back"]},
                "target": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    "browser_evaluate": {
        "params": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
            },
            "required": ["script"],
        },
    },
    "schedule_cron": {
        "params": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "when": {"type": "string"},
                "summary": {"type": "string"},
                "recurring": {"type": "boolean"},
                "interval_minutes": {"type": "integer"},
                "creator_number": {"type": "string"},
            },
            "required": ["prompt", "when", "summary", "creator_number"],
        },
    },
    "list_cronjobs": {
        "params": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "job_id": {"type": "string"},
            },
        },
    },
    "cancel_cronjob": {
        "params": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
            },
            "required": ["job_id"],
        },
    },
    "spawn_subagent": {
        "params": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    "stop_running_activity": {
        "params": {
            "type": "object",
            "properties": {
                "activity_id": {"type": "string"},
            },
            "required": ["activity_id"],
        },
    },
    "update_soul": {
        "params": {
            "type": "object",
            "properties": {
                "new_content": {"type": "string"},
            },
            "required": ["new_content"],
        },
    },
    "read_soul": {
        "params": {
            "type": "object",
            "properties": {},
        },
    },
    "recall_memories": {
        "params": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "memory_type": {"type": "string", "enum": ["preference", "feedback", "topic", "fact"]},
            },
        },
    },
    "remember": {
        "params": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "memory_type": {"type": "string", "enum": ["preference", "feedback", "topic", "fact"]},
            },
            "required": ["content", "memory_type"],
        },
    },
    "analyze_image": {
        "params": {
            "type": "object",
            "properties": {
                "image_filename": {"type": "string"},
                "image_query": {"type": "string"},
            },
            "required": ["image_filename", "image_query"],
        },
    },
    "get_skill": {
        "params": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    "send_signal_attachment": {
        "params": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "caption": {"type": "string"},
            },
            "required": ["filename"],
        },
    },
    "generate_image": {
        "params": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "input_images": {"type": "array", "items": {"type": "string"}},
                "size": {"type": "string"},
                "steps": {"type": "integer", "minimum": 1},
            },
            "required": ["prompt"],
        },
    },
    "text_to_speech": {
        "params": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "language": {"type": "string"},
                "voice": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    "list_tts_voices": {
        "params": {
            "type": "object",
            "properties": {
                "language": {"type": "string"},
            },
            "required": [],
        },
    },
    "set_tts_voice": {
        "params": {
            "type": "object",
            "properties": {
                "language": {"type": "string"},
                "voice": {"type": "string"},
            },
            "required": ["language"],
        },
    },
    "retranscribe_audio": {
        "params": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "language": {"type": "string"},
            },
            "required": ["filename"],
        },
    },
    "set_channel_mode": {
        "params": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["mention_required", "always_process"]
                },
            },
            "required": ["mode"],
        },
    },
    "get_channel_mode": {
        "params": {
            "type": "object",
            "properties": {},
        },
    },
    "end_call": {
        "params": {
            "type": "object",
            "properties": {},
        },
    },
    "set_message_mode": {
        "params": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["off", "append", "replace"]
                },
            },
            "required": ["mode"],
        },
    },
    "get_message_mode": {
        "params": {
            "type": "object",
            "properties": {},
        },
    },
    "debug_cron": {
        "params": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    "debug_subagent": {
        "params": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
}


def register_all_tools(registry: ToolRegistry) -> None:
    registry.register(
        "file_read",
        files.file_read,
        TOOL_DEFINITIONS["file_read"]["params"],
    )
    registry.register(
        "file_write",
        files.file_write,
        TOOL_DEFINITIONS["file_write"]["params"],
    )
    registry.register(
        "file_grep",
        files.file_grep,
        TOOL_DEFINITIONS["file_grep"]["params"],
    )
    registry.register(
        "file_list",
        files.file_list,
        TOOL_DEFINITIONS["file_list"]["params"],
    )
    registry.register(
        "file_search",
        files.file_search,
        TOOL_DEFINITIONS["file_search"]["params"],
    )
    registry.register(
        "message_search",
        messages.message_search,
        TOOL_DEFINITIONS["message_search"]["params"],
    )
    registry.register(
        "bash",
        bash.bash,
        TOOL_DEFINITIONS["bash"]["params"],
    )
    _browser_handlers: dict[str, Callable]
    if BROWSE_AVAILABLE:
        _browser_handlers = {
            "browser_navigate": browse.browser_navigate,
            "web_search": browse.web_search,
            "browser_screenshot": browse.browser_screenshot,
            "browser_interact": browse.browser_interact,
            "browser_evaluate": browse.browser_evaluate,
        }
    else:
        _browser_handlers = {
            "browser_navigate": _stub_browser_navigate,
            "web_search": _stub_web_search,
            "browser_screenshot": _stub_browser_screenshot,
            "browser_interact": _stub_browser_interact,
            "browser_evaluate": _stub_browser_evaluate,
        }
    for _name, _handler in _browser_handlers.items():
        registry.register(
            _name,
            _handler,
            TOOL_DEFINITIONS[_name]["params"],
        )
    registry.register(
        "schedule_cron",
        cron.schedule_cron,
        TOOL_DEFINITIONS["schedule_cron"]["params"],
    )
    registry.register(
        "list_cronjobs",
        cron.list_cronjobs,
        TOOL_DEFINITIONS["list_cronjobs"]["params"],
    )
    registry.register(
        "cancel_cronjob",
        cron.cancel_cronjob,
        TOOL_DEFINITIONS["cancel_cronjob"]["params"],
    )
    registry.register(
        "spawn_subagent",
        cron.spawn_subagent,
        TOOL_DEFINITIONS["spawn_subagent"]["params"],
    )
    registry.register(
        "stop_running_activity",
        cron.stop_running_activity,
        TOOL_DEFINITIONS["stop_running_activity"]["params"],
    )
    registry.register(
        "update_soul",
        soul.update_soul,
        TOOL_DEFINITIONS["update_soul"]["params"],
    )
    registry.register(
        "read_soul",
        soul.read_soul,
        TOOL_DEFINITIONS["read_soul"]["params"],
    )
    registry.register(
        "recall_memories",
        memory.recall_memories,
        TOOL_DEFINITIONS["recall_memories"]["params"],
    )
    registry.register(
        "remember",
        memory.remember,
        TOOL_DEFINITIONS["remember"]["params"],
    )
    registry.register(
        "analyze_image",
        files.analyze_image,
        TOOL_DEFINITIONS["analyze_image"]["params"],
    )
    registry.register(
        "get_skill",
        skills.get_skill,
        TOOL_DEFINITIONS["get_skill"]["params"],
    )
    registry.register(
        "send_signal_attachment",
        signal_ops.send_signal_attachment,
        TOOL_DEFINITIONS["send_signal_attachment"]["params"],
    )
    registry.register(
        "generate_image",
        imagegen_ops.generate_image,
        TOOL_DEFINITIONS["generate_image"]["params"],
    )
    registry.register(
        "text_to_speech",
        tts_ops.text_to_speech,
        TOOL_DEFINITIONS["text_to_speech"]["params"],
    )
    registry.register(
        "list_tts_voices",
        tts_ops.list_tts_voices,
        TOOL_DEFINITIONS["list_tts_voices"]["params"],
    )
    registry.register(
        "set_tts_voice",
        tts_ops.set_tts_voice,
        TOOL_DEFINITIONS["set_tts_voice"]["params"],
    )
    registry.register(
        "retranscribe_audio",
        stt_ops.retranscribe_audio,
        TOOL_DEFINITIONS["retranscribe_audio"]["params"],
    )
    registry.register(
        "set_channel_mode",
        channel_mode.set_channel_mode,
        TOOL_DEFINITIONS["set_channel_mode"]["params"],
    )
    registry.register(
        "get_channel_mode",
        channel_mode.get_channel_mode,
        TOOL_DEFINITIONS["get_channel_mode"]["params"],
    )
    registry.register(
        "end_call",
        voice_call.end_call,
        TOOL_DEFINITIONS["end_call"]["params"],
    )
    registry.register(
        "set_message_mode",
        message_mode.set_message_mode,
        TOOL_DEFINITIONS["set_message_mode"]["params"],
    )
    registry.register(
        "get_message_mode",
        message_mode.get_message_mode,
        TOOL_DEFINITIONS["get_message_mode"]["params"],
    )
    registry.register(
        "debug_cron",
        debug_cron.debug_cron,
        TOOL_DEFINITIONS["debug_cron"]["params"],
    )
    registry.register(
        "debug_subagent",
        debug_subagent.debug_subagent,
        TOOL_DEFINITIONS["debug_subagent"]["params"],
    )


