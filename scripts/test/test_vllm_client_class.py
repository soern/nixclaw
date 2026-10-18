#!/usr/bin/env python3
"""Test VLLMClient class integration."""

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.nixclaw_agent.config import VllmConfig, VllmOptionsConfig
from src.nixclaw_agent.llm import VLLMClient
from src.nixclaw_agent.types import Tool


async def test_vllm_client():
    """Test VLLMClient class with actual API call."""
    print("\n=== Testing VLLMClient Class ===")
    
    config = VllmConfig(
        host="http://gx10-811e.lan:8000",
        model="RedHatAI/gemma-4-31B-it-NVFP4",
        options=VllmOptionsConfig(
            temperature=1.0,
            top_p=0.95,
            top_k=64,
            max_tokens=100,
            timeout=60.0,
        ),
    )
    
    client = VLLMClient(config)
    
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is 3+3? Answer briefly."},
    ]
    
    tools = []
    
    text_response, tool_calls = await client.chat(messages, tools)
    
    print(f"Response: {text_response}")
    print(f"Tool calls: {tool_calls}")
    
    assert text_response, "Expected a text response"
    print("\nVLLMClient test PASSED")


if __name__ == "__main__":
    asyncio.run(test_vllm_client())
