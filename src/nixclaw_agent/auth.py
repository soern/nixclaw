"""User authentication and authorization management."""

from src.nixclaw_agent.config import AuthConfig
from src.nixclaw_agent.types import UserTier


TOOL_PERMISSIONS: dict[UserTier, set[str]] = {
    UserTier.PRIVILEGED: {
        "file_read",
        "file_write",
        "file_grep",
        "file_list",
        "file_search",
        "message_search",
        "bash",
        "browser_navigate",
        "browser_screenshot",
        "browser_interact",
        "browser_evaluate",
        "web_search",
        "schedule_cron",
        "list_cronjobs",
        "cancel_cronjob",
        "spawn_subagent",
        "stop_running_activity",
        "update_soul",
        "read_soul",
        "remember",
        "recall_memories",
        "analyze_image",
        "get_skill",
        "send_signal_attachment",
        "generate_image",
        "text_to_speech",
        "list_tts_voices",
        "set_tts_voice",
        "retranscribe_audio",
        "get_channel_mode",
        "set_channel_mode",
        "get_message_mode",
        "set_message_mode",
        "end_call",
        "debug_cron",
        "debug_subagent",
    },
    UserTier.KNOWN: {
        "file_read",
        "file_write",
        "file_grep",
        "file_list",
        "file_search",
        "message_search",
        "browser_navigate",
        "browser_screenshot",
        "browser_interact",
        "browser_evaluate",
        "web_search",
        "read_soul",
        "analyze_image",
        "get_skill",
        "send_signal_attachment",
        "generate_image",
        "text_to_speech",
        "list_tts_voices",
        "set_tts_voice",
        "retranscribe_audio",
        "get_channel_mode",
        "set_channel_mode",
        "get_message_mode",
        "set_message_mode",
        "end_call",
    },
    UserTier.UNKNOWN: {"get_skill"},
}


class AuthManager:
    def __init__(self, config: AuthConfig):
        self.update(config)

    def update(self, config: AuthConfig) -> None:
        """Replace the privileged/known number sets (used by config reload)."""
        self._privileged_numbers = set(config.privileged_numbers)
        self._known_numbers = set(config.known_numbers)

    def get_user_tier(self, phone_number: str) -> UserTier:
        if phone_number in self._privileged_numbers:
            return UserTier.PRIVILEGED
        if phone_number in self._known_numbers:
            return UserTier.KNOWN
        return UserTier.UNKNOWN

    def can_use_tool(self, tier: UserTier, tool_name: str) -> bool:
        return tool_name in TOOL_PERMISSIONS[tier]

    def get_allowed_tools(self, tier: UserTier) -> set[str]:
        return TOOL_PERMISSIONS[tier]
