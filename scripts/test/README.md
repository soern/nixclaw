# Manual Integration Tests

These are manual integration tests that require external infrastructure and cannot run in automated CI/CD pipelines.

## Prerequisites

- vLLM server running at `http://gx10-811e.lan:8000`
- Python 3.8+
- `httpx` package installed

## Test Files

### test_discard_keyword_manual.py
Tests the `__DISCARD_OUTPUT__` keyword detection in LLM responses.

**Requirements:** vLLM instance with RedHatAI/gemma-4-31B-it-NVFP4 model

**Usage:**
```bash
python3 test_discard_keyword_manual.py
```

**Coverage:** ~22 test cases covering:
- Cron jobs with no new information (should discard)
- Cron jobs with new information (should not discard)
- Different content patterns and contexts

### test_llm_discard_manual.py
Comprehensive test suite for `__DISCARD_OUTPUT__`, `__MUTE__`, and `__UNMUTE__` keyword handling in LLM responses.

**Requirements:** vLLM instance with RedHatAI/gemma-4-31B-it-NVFP4 model

**Usage:**
```bash
python3 test_llm_discard_manual.py
```

**Coverage:** ~20 test cases covering:
- __DISCARD_OUTPUT__ in various contexts
- __UNMUTE__ triggering
- __MUTE__ triggering
- Expected vs. actual keyword presence

## Skipping on Missing Infrastructure

Both test files include automatic skip logic. If the vLLM server at `gx10-811e.lan:8000` is not available:

```
SKIP: vLLM server not available at gx10-811e.lan:8000
This is a manual integration test requiring external infrastructure.
```

The test script exits cleanly with status 0 (success) so CI/CD pipelines are not broken.

## Automation Notes

- These tests are **NOT** included in `pytest tests/` runs
- They require manual execution or explicit invocation via `pytest scripts/test/`
- Timeout is set to 120 seconds per LLM request
- Failures are NOT considered CI failures due to external infrastructure dependency

## Consolidation Notes

Both test files cover similar scenarios but with different test matrices:
- `test_discard_keyword_manual.py` focuses on cron/baseline scenarios
- `test_llm_discard_manual.py` provides comprehensive keyword interaction testing

For future work, consider:
1. Deduplicate overlapping test cases (currently ~42 total)
2. Extract common test infrastructure into a shared helper module
3. Unify test reporting format
4. Add support for parametrized test selection (run only "A" category, "B" category, etc.)
