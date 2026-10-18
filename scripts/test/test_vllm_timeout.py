#!/usr/bin/env python3
"""
Test script to replicate the exact httpx call to vLLM that the nixclaw agent uses.
This script is standalone and can be run without Docker.
"""

import httpx
import logging
import sys
import traceback
import json
import asyncio
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("test_vllm")


# Exact configuration from VllmConfig and VllmOptionsConfig
VLLM_HOST = "http://gx10-811e.lan:8000"
MODEL = "RedHatAI/gemma-4-31B-it-NVFP4"
TIMEOUT = 120.0  # Default from VllmOptionsConfig

# Tool definitions (same format as TOOL_DEFINITIONS in tools/__init__.py)
CRONJOBS_TOOL = {
    "list_cronjobs": {
        "description": "List all scheduled cron jobs for this channel. Shows job ID, prompt, scheduled time, status, and whether it's recurring.",
        "params": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Optional filter by status: pending, running, complete, failed"},
            },
        },
    },
}


def create_timeout() -> httpx.Timeout:
    """Create the httpx.Timeout object exactly as done in llm.py line 437."""
    timeout = httpx.Timeout(
        timeout=TIMEOUT,  # total timeout
        connect=TIMEOUT   # connection timeout
    )
    logger.info(f"Created httpx.Timeout object: {timeout}")
    logger.info(f"  - timeout.connect: {timeout.connect}")
    logger.info(f"  - timeout.read: {timeout.read}")
    logger.info(f"  - timeout.write: {timeout.write}")
    logger.info(f"  - timeout.pool: {timeout.pool}")
    return timeout


def build_payload() -> dict:
    """Build the exact payload sent to vLLM."""
    # Simulate messages like what the agent would send
    messages = [
        {
            "role": "user",
            "content": "list my cronjobs"
        }
    ]
    
    # Transform for vLLM (same logic as _transform_messages_for_vllm)
    # In this case, simple text message, no transformation needed
    
    # Build tool definitions (same format as in llm.py chat method)
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": tool["description"],
                "parameters": tool["params"],
            },
        }
        for name, tool in CRONJOBS_TOOL.items()
    ]
    
    # Build payload exactly as in llm.py lines 425-434
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 2048,           # default from VllmOptionsConfig
        "temperature": 1.0,           # default from VllmOptionsConfig
        "top_p": 0.95,                 # default from VllmOptionsConfig
        "top_k": 64,                   # default from VllmOptionsConfig
    }
    if tool_defs:
        payload["tools"] = tool_defs
    
    return payload


async def make_request():
    """Make the async request to vLLM."""
    timeout_obj = create_timeout()
    payload = build_payload()
    
    url = f"{VLLM_HOST}/v1/chat/completions"
    logger.info(f"--- Making POST request to: {url} ---")
    logger.info(f"Request timeout: {timeout_obj}")
    logger.info("")
    
    logger.info("Creating AsyncClient with timeout...")
    async with httpx.AsyncClient(timeout=timeout_obj) as client:
        logger.info("AsyncClient created successfully")
        logger.info("Sending POST request...")
        
        # Log connection attempt
        logger.info(f"[Connection] Attempting to connect to {VLLM_HOST}")
        
        response = await client.post(url, json=payload)
        
        logger.info(f"[Connection] Connected successfully")
        logger.info(f"[Response] Status code: {response.status_code}")
        
        response.raise_for_status()
        data = response.json()
        
        logger.info("[Success] Received response from vLLM:")
        print(json.dumps(data, indent=2))
        
        return data


async def main():
    logger.info("=" * 80)
    logger.info("VLLM Timeout Test Script")
    logger.info("=" * 80)
    logger.info(f"Timestamp: {datetime.now().isoformat()}")
    logger.info(f"VLLM Host: {VLLM_HOST}")
    logger.info(f"Model: {MODEL}")
    logger.info(f"Timeout: {TIMEOUT}s")
    logger.info("")
    
    # Create timeout object
    logger.info("--- Creating httpx.Timeout object ---")
    timeout_obj = create_timeout()
    logger.info("")
    
    # Build payload
    logger.info("--- Building payload ---")
    payload = build_payload()
    logger.info(f"Payload structure:")
    logger.info(f"  model: {payload['model']}")
    logger.info(f"  messages count: {len(payload['messages'])}")
    logger.info(f"  max_tokens: {payload['max_tokens']}")
    logger.info(f"  temperature: {payload['temperature']}")
    logger.info(f"  top_p: {payload['top_p']}")
    logger.info(f"  top_k: {payload['top_k']}")
    logger.info(f"  tools count: {len(payload.get('tools', []))}")
    logger.info("")
    
    logger.info("Full payload JSON:")
    print(json.dumps(payload, indent=2))
    logger.info("")
    
    # Log the request that will be made
    url = f"{VLLM_HOST}/v1/chat/completions"
    logger.info(f"--- Making POST request to: {url} ---")
    logger.info(f"Request timeout: {timeout_obj}")
    logger.info("")
    
    # Make the request with extensive error handling
    try:
        await make_request()
        
    except httpx.TimeoutException as e:
        logger.error("=" * 80)
        logger.error("TIMEOUT EXCEPTION CAUGHT!")
        logger.error("=" * 80)
        logger.error(f"Timeout type: {type(e).__name__}")
        logger.error(f"Error message: {str(e)}")
        logger.error("")
        logger.error("Timeout object details:")
        logger.error(f"  connect timeout: {timeout_obj.connect}")
        logger.error(f"  read timeout: {timeout_obj.read}")
        logger.error(f"  total timeout: {timeout_obj.timeout}")
        logger.error("")
        logger.error("Full traceback:")
        traceback.print_exc()
        logger.error("")
        logger.error("This indicates that vLLM is not responding within the 120s timeout.")
        logger.error("Possible reasons:")
        logger.error("  1. vLLM server is not running or unreachable")
        logger.error("  2. The model is taking too long to respond")
        logger.error("  3. Network connectivity issues")
        return 1
        
    except httpx.HTTPStatusError as e:
        logger.error("=" * 80)
        logger.error("HTTP STATUS ERROR!")
        logger.error("=" * 80)
        logger.error(f"Status code: {e.response.status_code}")
        logger.error(f"Response text: {e.response.text}")
        traceback.print_exc()
        return 1
        
    except httpx.ConnectError as e:
        logger.error("=" * 80)
        logger.error("CONNECTION ERROR!")
        logger.error("=" * 80)
        logger.error(f"Error message: {str(e)}")
        logger.error("")
        logger.error("This indicates the server is not reachable.")
        logger.error("Possible reasons:")
        logger.error("  1. vLLM server is not running")
        logger.error("  2. The host/port is incorrect")
        logger.error("  3. Firewall is blocking the connection")
        logger.error("")
        traceback.print_exc()
        return 1
        
    except Exception as e:
        logger.error("=" * 80)
        logger.error(f"UNEXPECTED ERROR: {type(e).__name__}")
        logger.error("=" * 80)
        logger.error(f"Error message: {str(e)}")
        traceback.print_exc()
        return 1
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("Test completed successfully!")
    logger.info("=" * 80)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
