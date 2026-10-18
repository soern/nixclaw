#!/usr/bin/env python3
"""Manual integration test for __DISCARD_OUTPUT__ LLM keyword handling.

This test requires a vLLM instance running at http://gx10-811e.lan:8000.
It is NOT part of the automated test suite (requires external LLM infrastructure).
Run manually only when the external LLM is available.
"""
import httpx
import json
from datetime import datetime
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

LLM_URL = "http://gx10-811e.lan:8000/v1/chat/completions"
MODEL = "RedHatAI/gemma-4-31B-it-NVFP4"

SYSTEM_PROMPT = """You are Nixclaw, a helpful AI assistant.

You have access to the following tools:
- No tools available for this test

User tier: Privileged
- Privileged: Full access to all tools

★ USER IDENTITY:
The user's phone number is: +000000
This phone number comes from the source field of each incoming message - you do NOT need to ask for it or store it.

★ CRITICAL INSTRUCTION: You are in an ongoing conversation. Use the message history provided to maintain context.

★ CURRENT DATETIME: 2026-04-08 12:00:00
★ YOUR TIMEZONE: CET"""

TESTS = [
    {
        "id": 1,
        "category": "A1 - should discard",
        "prompt": """You are performing automated news research. Your goal is to check for NEW information only.
CRON RESEARCH TASK: Check tech news for today. Look for any NEW headlines that are not already known.
If you find NO new items worth reporting, include the keyword __DISCARD_OUTPUT__ in your response.
If there are new items, report them briefly.""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 2,
        "category": "A2 - should discard",
        "prompt": """Research current events news. Compare against the baseline which already contains:
- "Tech stocks rise on AI optimism"
- "New climate agreement signed"

If your findings are ALL already in the baseline above, include __DISCARD_OUTPUT__.
Otherwise, report the new items.""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 3,
        "category": "A3 - should discard",
        "prompt": """You are a news monitoring agent. You checked the following sources:
- News site A: No new articles since yesterday
- News site B: Same headlines as last check

If nothing new was found, include __DISCARD_OUTPUT__ in your response.""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 4,
        "category": "A4 - should discard",
        "prompt": """This is an automated check. You verified: no price changes, no new announcements, no updates.
If there is genuinely nothing new to report, write ONLY: __DISCARD_OUTPUT__""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 5,
        "category": "B1 - should NOT discard",
        "prompt": """You are performing automated news research.
CRON RESEARCH TASK: Check tech news. You found ONE new headline: "OpenAI announces GPT-5"
This is genuinely new and not in any baseline.
Report this headline briefly WITHOUT including __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 6,
        "category": "B2 - should NOT discard",
        "prompt": """News check: You discovered a major update not previously reported: "Major earthquake in Japan"
This is significant news that the user would want to know about.
Respond with a brief summary of this news. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 7,
        "category": "B3 - should NOT discard",
        "prompt": """You checked news for topics the user cares about. You found: "New Python version released with async improvements"
The user specifically wanted to know about Python updates.
Report this finding. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 8,
        "category": "C1 - muted channel, no news, should NOT unmute",
        "prompt": """You are woken by an automated cronjob. This channel is currently muted.
You checked news and found no new items.
Your instructions say: NEVER use __UNMUTE__ to unmute a channel when woken from cron.
If there are no new items, include __DISCARD_OUTPUT__ and stay silent.
Do NOT attempt to unmute this channel.""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 9,
        "category": "C2 - muted channel with news, should report but NOT unmute",
        "prompt": """You are woken by an automated cronjob. This channel is muted.
You found news: "Stock market hits new record high"
Your instructions: Never unmute when woken from cron. Report if news, but don't unmute.
Write a brief report. Do NOT include __DISCARD_OUTPUT__. Do NOT include __UNMUTE__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 10,
        "category": "D1 - normal conversation",
        "prompt": """The user says: "Hello, how are you today?"
This is a normal conversation, not a cron job.
Respond naturally without including __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 11,
        "category": "D2 - user asking for news",
        "prompt": """User asks: "Can you check the news for me?"
This is a direct user request, not an automated cron check.
Give a brief response. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 12,
        "category": "D3 - user asks to be quiet - should trigger __MUTE__",
        "prompt": """The user says: "Please be quiet now"
Your instruction for __MUTE__: Include __MUTE__ when user asks you to stop talking.
Do NOT include __DISCARD_OUTPUT__ in this case.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": True,
    },
    {
        "id": 13,
        "category": "E1 - partial news",
        "prompt": """You found some items already in baseline AND one new item: "New electric car model announced"
Your job is to report the new item only.
Respond with the new item. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 14,
        "category": "E3 - multiple new items",
        "prompt": """You found THREE new items:
1. "Tech company X releases new product"
2. "New study shows AI progress"
3. "Market update"

Report all new items briefly. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 15,
        "category": "F1 - opinion response",
        "prompt": """User asks: "What's your favorite programming language?"
Give your opinion. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 16,
        "category": "F2 - thanks response",
        "prompt": """User says: "Thank you for the help!"
Respond to the thanks. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 17,
        "category": "F4 - explicit user request",
        "prompt": """User says: "Tell me a joke"
Tell a joke. Do NOT include __DISCARD_OUTPUT__.""",
        "expected_discard": False,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 18,
        "category": "NEW - Simple discard instruction",
        "prompt": """Check for news. If there is no news, include __DISCARD_OUTPUT__ in your response.""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 19,
        "category": "NEW - Direct keyword test",
        "prompt": """Respond with exactly the word "test" followed by __DISCARD_OUTPUT__ if you understand.""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
    {
        "id": 20,
        "category": "NEW - Plain discard without context",
        "prompt": """Nothing new happened. Include __DISCARD_OUTPUT__ in your response.""",
        "expected_discard": True,
        "expected_unmute": False,
        "expected_mute": False,
    },
]

def call_llm(prompt):
    try:
        response = httpx.post(
            LLM_URL,
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 1.0,
                "top_p": 0.95,
                "max_tokens": 2048
            },
            timeout=120.0
        )
        result = response.json()
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"ERROR: {e}"

def check_keyword(text, keyword):
    return keyword in text if text else False

def run_tests():
    print("=" * 80)
    print("LLM KEYWORD BEHAVIOR TEST")
    print(f"Model: {MODEL}")
    print(f"Time: {datetime.now()}")
    print("=" * 80)
    
    results = []
    
    for test in TESTS:
        print(f"\n{'='*80}")
        print(f"TEST {test['id']}: {test['category']}")
        print(f"{'='*80}")
        print(f"Prompt:\n{test['prompt'][:200]}...")
        
        response = call_llm(test['prompt'])
        
        has_discard = check_keyword(response, "__DISCARD_OUTPUT__")
        has_unmute = check_keyword(response, "__UNMUTE__")
        has_mute = check_keyword(response, "__MUTE__")
        
        discard_correct = has_discard == test['expected_discard']
        unmute_correct = has_unmute == test['expected_unmute']
        mute_correct = has_mute == test['expected_mute']
        
        overall_pass = discard_correct and unmute_correct and mute_correct
        
        print(f"\nResponse:\n{response}")
        print(f"\n--- Analysis ---")
        print(f"__DISCARD_OUTPUT__ present: {has_discard} (expected: {test['expected_discard']}) {'✓' if discard_correct else '✗'}")
        print(f"__UNMUTE__ present: {has_unmute} (expected: {test['expected_unmute']}) {'✓' if unmute_correct else '✗'}")
        print(f"__MUTE__ present: {has_mute} (expected: {test['expected_mute']}) {'✓' if mute_correct else '✗'}")
        print(f"Overall: {'PASS' if overall_pass else 'FAIL'}")
        
        results.append({
            "id": test['id'],
            "category": test['category'],
            "response": response,
            "has_discard": has_discard,
            "has_unmute": has_unmute,
            "has_mute": has_mute,
            "discard_correct": discard_correct,
            "unmute_correct": unmute_correct,
            "mute_correct": mute_correct,
            "pass": overall_pass,
        })
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    passed = sum(1 for r in results if r['pass'])
    failed = len(results) - passed
    
    print(f"Passed: {passed}/{len(results)}")
    print(f"Failed: {failed}/{len(results)}")
    
    print("\n--- DISCARD_OUTPUT Tests ---")
    discard_tests = [r for r in results if r['category'].startswith(('A', 'NEW'))]
    for r in discard_tests:
        status = "✓" if r['discard_correct'] else "✗"
        print(f"Test {r['id']}: {r['category']} - {'PASS' if r['discard_correct'] else 'FAIL'} {status}")
        if not r['discard_correct']:
            print(f"  Expected discard={r['category'].find('NOT') == -1 and 'should discard' in r['category']}, Got={r['has_discard']}")
    
    print("\n--- Keyword Mechanism Understanding ---")
    discard_understanding = sum(1 for r in results if r['discard_correct'])
    print(f"__DISCARD_OUTPUT__ correctly handled: {discard_understanding}/{len(results)}")
    
    return results

if __name__ == "__main__":
    run_tests()