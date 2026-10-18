#!/usr/bin/env python3
"""Test chat functionality with remote vLLM instance."""

import asyncio
import time
import httpx


VLLM_HOST = "http://gx10-811e.lan:8000"
MODEL = "RedHatAI/gemma-4-31B-it-NVFP4"

TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 64


async def test_chat():
    """Test basic chat completion."""
    print("\n=== Testing Chat ===")
    
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is 2+2? Answer briefly."},
    ]
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        start_time = time.perf_counter()
        response = await client.post(
            f"{VLLM_HOST}/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": messages,
                "max_tokens": 100,
                "temperature": TEMPERATURE,
                "top_p": TOP_P,
                "top_k": TOP_K,
            },
        )
        end_time = time.perf_counter()
        inference_time_ms = (end_time - start_time) * 1000
        
        response.raise_for_status()
        data = response.json()
        
        usage = data.get('usage', {})
        prompt_tokens = usage.get('prompt_tokens', 0)
        completion_tokens = usage.get('completion_tokens', 0)
        
        print(f"Response: {data['choices'][0]['message']['content']}")
        print(f"Model: {data['model']}")
        print(f"Inference time: {inference_time_ms:.2f} ms")
        print(f"Tokens - prompt: {prompt_tokens}, completion: {completion_tokens}")
        if completion_tokens > 0:
            print(f"Tokens/sec: {completion_tokens / (inference_time_ms / 1000):.2f}")


if __name__ == "__main__":
    asyncio.run(test_chat())
