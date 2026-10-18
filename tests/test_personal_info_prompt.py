#!/usr/bin/env python3
"""Tests for personal info lookup in system prompt - ensures agent uses message_search."""

import pytest

from src.nixclaw_agent.llm import LLMClient
from src.nixclaw_agent.config import OllamaConfig, OllamaOptionsConfig
from src.nixclaw_agent.types import Tool


class TestPersonalInfoPrompt:
    """Test that system prompt contains correct personal info instructions based on tools."""

    @pytest.fixture
    def llm_client(self):
        """Create LLM client for testing."""
        config = OllamaConfig(
            host="http://localhost:11434",
            model="test-model",
            options=OllamaOptionsConfig()
        )
        return LLMClient(config)

    def test_prompt_with_message_search_includes_verification_instruction(self, llm_client):
        """When message_search is available, prompt should instruct to use it for personal info."""
        tools = [
            Tool(
                name="message_search",
                description="Search message history",
                parameters={"type": "object", "properties": {}}
            ),
            Tool(
                name="file_read",
                description="Read files",
                parameters={"type": "object", "properties": {}}
            )
        ]
        
        prompt = llm_client.build_system_prompt(
            tools=tools,
            user_tier="Privileged",
            soul_content=None,
            memory_context=None,
            event_source="+1234567890"
        )
        
        assert "message_search" in prompt, "Prompt should mention message_search"
        assert "PERSONAL INFO" in prompt, "Prompt should contain PERSONAL INFO section"
        assert "MUST use message_search to verify" in prompt, \
            "Prompt should instruct to use message_search for personal details"
        assert "Never fabricate" in prompt, "Prompt should warn against fabricating"

    def test_prompt_without_message_search_includes_limitation_warning(self, llm_client):
        """When message_search is NOT available, prompt should warn about info limitations."""
        tools = [
            Tool(
                name="file_read",
                description="Read files",
                parameters={"type": "object", "properties": {}}
            )
        ]
        
        prompt = llm_client.build_system_prompt(
            tools=tools,
            user_tier="Unknown",
            soul_content=None,
            memory_context=None,
            event_source="+1234567890"
        )
        
        assert "INFORMATION LIMITATIONS" in prompt, \
            "Prompt should contain INFORMATION LIMITATIONS section"
        assert "don't have access to conversation history" in prompt, \
            "Prompt should explain lack of conversation history access"
        assert "say you don't know" in prompt, \
            "Prompt should instruct to say don't know when info cannot be verified"

    def test_prompt_empty_tools_includes_limitation_warning(self, llm_client):
        """When no tools available, prompt should warn about info limitations."""
        tools = []
        
        prompt = llm_client.build_system_prompt(
            tools=tools,
            user_tier="Unknown",
            soul_content=None,
            memory_context=None,
            event_source="+1234567890"
        )
        
        assert "INFORMATION LIMITATIONS" in prompt, \
            "Prompt should contain INFORMATION LIMITATIONS for unknown tier with no tools"

    def test_prompt_with_other_tools_but_not_message_search(self, llm_client):
        """Even with other tools available, without message_search should show limitation."""
        tools = [
            Tool(
                name="file_read",
                description="Read files",
                parameters={"type": "object", "properties": {}}
            ),
            Tool(
                name="bash",
                description="Execute bash commands",
                parameters={"type": "object", "properties": {}}
            )
        ]
        
        prompt = llm_client.build_system_prompt(
            tools=tools,
            user_tier="Privileged",
            soul_content=None,
            memory_context=None,
            event_source="+1234567890"
        )
        
        assert "INFORMATION LIMITATIONS" in prompt, \
            "Prompt should warn about info limitations even for privileged users without message_search"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])