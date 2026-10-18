#!/usr/bin/env python3
"""Test tool calling with remote vLLM instance.

NOTE: Tool calling requires vLLM server to be started with:
  --enable-auto-tool-choice --tool-call-parser <parser>

If not configured, this test will fail with 400 error.
"""

import asyncio
import time
import httpx


VLLM_HOST = "http://gx10-811e.lan:8000"
MODEL = "RedHatAI/gemma-4-31B-it-NVFP4"

TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 64


async def test_tool_call():
    print("\n=== Testing Tool Calling ===")
    
    messages = [
        {"role": "system", "content": "You have access to tools."},
        {"role": "user", "content": "What is the current weather in Berlin?"},
    ]
    
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "City name",
                        },
                    },
                    "required": ["city"],
                },
            },
        },
    ]
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        start_time = time.perf_counter()
        response = await client.post(
            f"{VLLM_HOST}/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": messages,
                "tools": tools,
                "max_tokens": 200,
                "temperature": TEMPERATURE,
                "top_p": TOP_P,
                "top_k": TOP_K,
            },
        )
        end_time = time.perf_counter()
        inference_time_ms = (end_time - start_time) * 1000
        
        if response.status_code != 200:
            print(f"Error {response.status_code}: {response.text}")
            return
        
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        
        usage = data.get('usage', {})
        prompt_tokens = usage.get('prompt_tokens', 0)
        completion_tokens = usage.get('completion_tokens', 0)
        
        print(f"Response: {message.get('content', '')}")
        print(f"Inference time: {inference_time_ms:.2f} ms")
        print(f"Tokens - prompt: {prompt_tokens}, completion: {completion_tokens}")
        
        if "tool_calls" in message:
            print(f"Tool calls: {message['tool_calls']}")
        else:
            print("No tool calls in response")


if __name__ == "__main__":
    asyncio.run(test_tool_call())
