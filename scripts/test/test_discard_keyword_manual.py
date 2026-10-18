#!/usr/bin/env python3
"""Manual integration test for __DISCARD_OUTPUT__ keyword detection.

This test requires a vLLM instance running at http://gx10-811e.lan:8000.
It is NOT part of the automated test suite (requires external LLM infrastructure).
Run manually only when the external LLM is available.
"""
import httpx
import json
from typing import Optional
import sys

# Skip if external LLM unavailable
def check_llm_available():
    try:
        import socket
        socket.create_connection(("gx10-811e.lan", 8000), timeout=2)
        return True
    except (socket.gaierror, socket.timeout, ConnectionRefusedError):
        return False

if __name__ == "__main__" and not check_llm_available():
    print("\nSKIP: vLLM server not available at gx10-811e.lan:8000")
    print("This is a manual integration test requiring external infrastructure.")
    sys.exit(0)

LLM_CONFIG = {
    "model": "RedHatAI/gemma-4-31B-it-NVFP4",
    "temperature": 1.0,
    "top_p": 0.95,
    "max_tokens": 2048
}

SYSTEM_PROMPT = """You are Nixclaw, a helpful AI assistant.

You have access to the following tools:
- No tools available for this test

User tier: Privileged
- Privileged: Full access to all tools

★ USER IDENTITY:
The user's phone number is: +000000

★ CRITICAL INSTRUCTION: You are in an ongoing conversation.

★ CURRENT DATETIME: 2026-04-08 12:00:00
★ YOUR TIMEZONE: CET

★ MUTE MODE: Your job is to recognize when the user wants YOU to stop talking.
When the user asks you to be quiet, silent, or to stop talking, include the keyword __MUTE__ in your TEXT RESPONSE.

★ UNMUTE MODE: Include __UNMUTE__ in your response when the user addresses YOU and asks for a response."""

TESTS = [
    ("A1", "Cron, no news - should include __DISCARD_OUTPUT__", 
     "[SYSTEM: You are woken by an automated cronjob. Check for news updates.]\nCheck news sites. Compare against baseline. Report if anything new.\nBaseline: Tech stocks up 2%. No other items."),
    
    ("A2", "Cron, no new items - should include __DISCARD_OUTPUT__",
     "[SYSTEM: Automated research task - no user interaction]\nYou wake from cron. You checked news. No new items since last scan.\nAll findings already in baseline. No changes to report."),
    
    ("A3", "Cron monitoring, no changes - should include __DISCARD_OUTPUT__",
     "[SYSTEM: This is a cron-triggered research task]\nResearch task: Check for price changes on monitored stocks.\nResult: No price changes detected. All within baseline range."),
    
    ("B1", "Cron with new headline - should NOT include __DISCARD_OUTPUT__",
     "[SYSTEM: Cron-triggered news check]\nYou wake from cron. You found ONE new headline: \"OpenAI announces GPT-5 today\"\nThis is not in baseline. Report it briefly."),
    
    ("B2", "Cron with significant news - should NOT include __DISCARD_OUTPUT__",
     "[SYSTEM: Automated news monitoring via cron]\nNews check: You discovered \"Major earthquake in Japan\" - not in baseline.\nBrief summary: [write summary]"),
    
    ("B3", "Cron with multiple new items - should NOT include __DISCARD_OUTPUT__",
     "[SYSTEM: Cron-triggered research]\nResearch found THREE new items not in baseline:\n1. \"Tech company X releases new product\"\n2. \"New study shows AI progress\"\n3. \"Market update\"\nReport all three briefly."),
    
    ("C1", "User asks news directly - must provide response, NO discard",
     "User: \"Hey, what's the latest news?\""),
    
    ("C2", "User requests research - must respond normally",
     "User: \"Can you check what's happening with AI news today?\""),
    
    ("C3", "User asks for help - must respond normally",
     "User: \"Help me research the latest developments in Python\""),
    
    ("C4", "User asks for updates - must respond normally",
     "User: \"Have there been any new developments in the Python world?\""),
    
    ("C5", "User wants report - must respond normally",
     "User: \"Give me a brief report on today's top tech news\""),
    
    ("D1", "Unclear if new - should NOT discard if uncertain",
     "[SYSTEM: Cron-triggered research]\nResearch task: Check news. You're not sure if \"forecast shows rain\" was in baseline.\nIf there's any doubt, what should you do?"),
    
    ("D4", "Cron on muted channel, no news - should NOT unmute",
     "[SYSTEM: Cron wake on muted channel]\nYou wake from cron on a muted channel. You found no new items.\nWhat do you do? (channel must stay muted)"),
    
    ("D5", "Cron on muted channel with news - should report but NOT unmute",
     "[SYSTEM: Cron wake on muted channel]\nYou wake from cron on a muted channel. News found: \"Stock market hits record high\"\nWhat do you do? (must NOT unmute channel)"),
    
    ("E1", "User says hello - normal response",
     "User: \"Hello! How are you?\""),
    
    ("E2", "User asks opinion - normal response",
     "User: \"What's your favorite programming language?\""),
    
    ("E3", "User asks for joke - normal response",
     "User: \"Tell me a joke\""),
    
    ("F1", "User asks to be quiet - should use __MUTE__",
     "User: \"Please be quiet now\""),
    
    ("F2", "User addresses agent - should use __UNMUTE__",
     "User: \"Nixclaw, are you there?\""),
    
    ("F3", "Cron should not unmute - no unmute keyword",
     "[SYSTEM: Cron-triggered task on unmuted channel]\nYou wake from cron. No new items. What do you output?"),
]

def call_llm(prompt: str) -> str:
    response = httpx.post(
        "http://gx10-811e.lan:8000/v1/chat/completions",
        json={
            **LLM_CONFIG,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        },
        timeout=120.0
    )
    result = response.json()
    return result["choices"][0]["message"]["content"]

def analyze_response(response: str):
    has_discard = "__DISCARD_OUTPUT__" in response
    has_mute = "__MUTE__" in response
    has_unmute = "__UNMUTE__" in response
    return has_discard, has_mute, has_unmute

def run_tests():
    results = []
    for test_id, description, prompt in TESTS:
        print(f"\n{'='*60}")
        print(f"Running Test {test_id}: {description}")
        print(f"{'='*60}")
        
        response = call_llm(prompt)
        has_discard, has_mute, has_unmute = analyze_response(response)
        
        results.append({
            "test_id": test_id,
            "description": description,
            "prompt": prompt,
            "response": response,
            "has_discard": has_discard,
            "has_mute": has_mute,
            "has_unmute": has_unmute
        })
        
        print(f"Response:\n{response}")
        print(f"\n__DISCARD_OUTPUT__: {has_discard} | __MUTE__: {has_mute} | __UNMUTE__: {has_unmute}")
    
    return results

if __name__ == "__main__":
    results = run_tests()
    
    print("\n\n" + "="*80)
    print("SUMMARY REPORT")
    print("="*80)
    
    for r in results:
        print(f"\nTest {r['test_id']}: {r['description']}")
        print(f"  __DISCARD_OUTPUT__: {r['has_discard']} | __MUTE__: {r['has_mute']} | __UNMUTE__: {r['has_unmute']}")
