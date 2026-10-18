#!/usr/bin/env python3
"""Test image recognition with remote vLLM instance."""

import asyncio
import base64
import time
import httpx
from pathlib import Path


VLLM_HOST = "http://gx10-811e.lan:8000"
MODEL = "RedHatAI/gemma-4-31B-it-NVFP4"

TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 64


def encode_image_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


async def test_vision():
    print("\n=== Testing Image Recognition ===")
    
    image_path = Path(__file__).parent / "test_image.png"
    if not image_path.exists():
        print(f"Test image not found: {image_path}")
        print("Creating a simple test image...")
        create_test_image(image_path)
    
    image_b64 = encode_image_base64(str(image_path))
    
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        },
    ]
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        start_time = time.perf_counter()
        response = await client.post(
            f"{VLLM_HOST}/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": messages,
                "max_tokens": 200,
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
        print(f"Inference time: {inference_time_ms:.2f} ms")
        print(f"Tokens - prompt: {prompt_tokens}, completion: {completion_tokens}")
        if completion_tokens > 0:
            print(f"Tokens/sec: {completion_tokens / (inference_time_ms / 1000):.2f}")


def create_test_image(path: Path):
    """Create a simple PNG test image using raw bytes."""
    import struct
    import zlib
    
    width, height = 100, 100
    pixel_data = b""
    for y in range(height):
        pixel_data += b"\xff\x00\x00"
        for x in range(width - 1):
            pixel_data += b"\xff\x00\x00"
    
    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk_len = struct.pack(">I", len(data))
        chunk_crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xffffffff)
        return chunk_len + chunk_type + data + chunk_crc
    
    png_signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = png_chunk(b"IHDR", ihdr_data)
    
    raw_data = b""
    for y in range(height):
        raw_data += b"\x00"
        for x in range(width):
            raw_data += b"\xff\x00\x00"
    compressed = zlib.compress(raw_data, 9)
    idat = png_chunk(b"IDAT", compressed)
    iend = png_chunk(b"IEND", b"")
    
    with open(path, "wb") as f:
        f.write(png_signature + ihdr + idat + iend)
    print(f"Created test image: {path}")


if __name__ == "__main__":
    asyncio.run(test_vision())
