"""Tests for VLLMClient._compute_max_tokens (llm.py):

- tool_calls JSON is folded into the input-token estimate.
- image_url parts add IMAGE_TOKEN_ESTIMATE per image.
- The safety margin scales with input size (SAFETY_MARGIN_RATIO) instead of
  the old flat SAFETY_MARGIN_TOKENS, but the flat value still acts as a floor
  for small inputs.
"""

import json
import math

from src.nixclaw_agent.config import VllmConfig, VllmOptionsConfig
from src.nixclaw_agent.llm import (
    LLMClient,
    VLLMClient,
    IMAGE_TOKEN_ESTIMATE,
    MIN_OUTPUT_TOKENS,
    SAFETY_MARGIN_RATIO,
    SAFETY_MARGIN_TOKENS,
    TOKEN_ESTIMATE_INFLATION,
)


def _make_client(context_window: int = 200_000, max_tokens: int = 300_000) -> VLLMClient:
    opts = VllmOptionsConfig(context_window=context_window, max_tokens=max_tokens)
    cfg = VllmConfig(host="http://x", model="m", options=opts)
    return VLLMClient(cfg)


def _expected_est_input_tokens(content_text: str, image_count: int = 0) -> int:
    """Mirror VLLMClient._compute_max_tokens' input-estimate formula."""
    raw_estimate = LLMClient.estimate_token_count([{"content": content_text}], "")
    est = math.ceil(raw_estimate * TOKEN_ESTIMATE_INFLATION)
    est += image_count * IMAGE_TOKEN_ESTIMATE
    return est


class TestToolCallsFoldedIntoEstimate:
    def test_tool_calls_content_reduces_available_output(self):
        client = _make_client()
        base_messages = [{"role": "user", "content": "hello"}]
        tool_call_messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "1", "function": {"name": "search", "arguments": {"query": "x" * 4000}}}
                ],
            }
        ]

        base_max_out = client._compute_max_tokens(base_messages, [])
        with_tool_calls_max_out = client._compute_max_tokens(tool_call_messages, [])

        # Previously tool_calls JSON was silently excluded from the estimate,
        # so it would not have reduced the output budget at all.
        assert with_tool_calls_max_out < base_max_out


class TestImageUrlPartsIncreaseEstimate:
    def test_images_reduce_max_tokens_proportionally(self):
        client = _make_client()

        def messages_with_n_images(n: int) -> list[dict]:
            return [{"role": "user", "content": "look at this", "images": ["Zm9v"] * n}]

        zero = client._compute_max_tokens(messages_with_n_images(0), [])
        two = client._compute_max_tokens(messages_with_n_images(2), [])
        five = client._compute_max_tokens(messages_with_n_images(5), [])

        assert zero - two == 2 * IMAGE_TOKEN_ESTIMATE
        assert zero - five == 5 * IMAGE_TOKEN_ESTIMATE


class TestSafetyMarginScaling:
    def test_margin_scales_above_flat_floor_for_large_input(self):
        client = _make_client(context_window=1_000_000, max_tokens=900_000)
        large_content = "a" * 400_000
        messages = [{"role": "user", "content": large_content}]

        max_out = client._compute_max_tokens(messages, [])

        content_text = large_content + json.dumps([])
        est_input_tokens = _expected_est_input_tokens(content_text)
        scaled_margin = math.ceil(est_input_tokens * SAFETY_MARGIN_RATIO)

        # The scaled margin must exceed the old flat floor for this input size.
        assert scaled_margin > SAFETY_MARGIN_TOKENS

        expected_max_out = max(
            MIN_OUTPUT_TOKENS,
            min(client.options.max_tokens, client.options.context_window - est_input_tokens - scaled_margin),
        )
        assert max_out == expected_max_out

        # Sanity: using the old flat margin would have yielded a larger budget.
        flat_margin_max_out = client.options.context_window - est_input_tokens - SAFETY_MARGIN_TOKENS
        assert max_out < flat_margin_max_out

    def test_flat_floor_still_applies_for_small_input(self):
        client = _make_client(context_window=200_000, max_tokens=300_000)
        messages = [{"role": "user", "content": "hi"}]

        max_out = client._compute_max_tokens(messages, [])

        content_text = "hi" + json.dumps([])
        est_input_tokens = _expected_est_input_tokens(content_text)
        scaled_margin = math.ceil(est_input_tokens * SAFETY_MARGIN_RATIO)

        # For a tiny input, 10% of it is far below the flat floor.
        assert scaled_margin < SAFETY_MARGIN_TOKENS

        expected_max_out = max(
            MIN_OUTPUT_TOKENS,
            min(client.options.max_tokens, client.options.context_window - est_input_tokens - SAFETY_MARGIN_TOKENS),
        )
        assert max_out == expected_max_out
