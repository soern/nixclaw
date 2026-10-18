#!/usr/bin/env python3
"""Test the remote FLUX.2 [klein] image generation service on the DGX Spark.

Checks /health, /v1/models, then runs a generation and an edit round-trip,
saving the results to /tmp so you can eyeball quality and record benchmark
numbers for the table in deploy/dgx-spark/flux2-klein/README.md.

Usage:
  python3 scripts/test/test_flux2_klein.py [SIZE]
  SIZE defaults to 1024x1024; max is 2048x2048 (server cap), both dims
  should be multiples of 16. Try 1536x1024 before 2048x2048 - latent
  tokens scale quadratically and attention is O(n^2).
"""

import asyncio
import base64
import sys
import time

import httpx

FLUX_HOST = "http://gx10-811e.lan:8002"

PROMPT = "a red bicycle leaning against a brick wall, soft morning light"
EDIT_PROMPT = "make the bicycle blue"
SIZE = sys.argv[1] if len(sys.argv) > 1 else "1024x1024"
STEPS = 4
SEED = 42

# Generous: first generation after a restart pays JIT-compile costs.
TIMEOUT = 900.0

GEN_OUT = f"/tmp/flux2_klein_generated_{SIZE}.png"
EDIT_OUT = f"/tmp/flux2_klein_edited_{SIZE}.png"

TIMINGS: dict[str, float] = {}


async def check_health(client: httpx.AsyncClient) -> dict:
    print("\n=== /health ===")
    resp = await client.get(f"{FLUX_HOST}/health")
    resp.raise_for_status()
    data = resp.json()
    print(f"model:           {data['model']}")
    print(f"quant_mode:      {data['quant_mode']}")
    print(f"steps default:   {data['steps']}")
    print(f"resident VRAM:   {data.get('resident_memory_mb')} MB")
    return data


async def check_models(client: httpx.AsyncClient) -> None:
    print("\n=== /v1/models ===")
    resp = await client.get(f"{FLUX_HOST}/v1/models")
    resp.raise_for_status()
    for m in resp.json()["data"]:
        print(f"model: {m['id']} (quant_mode={m.get('quant_mode')})")


async def generate_image(client: httpx.AsyncClient, out_path: str) -> None:
    print(f"\n=== POST /v1/images/generations (prompt={PROMPT!r}) ===")
    start = time.perf_counter()
    resp = await client.post(
        f"{FLUX_HOST}/v1/images/generations",
        json={
            "prompt": PROMPT,
            "size": SIZE,
            "n": 1,
            "response_format": "b64_json",
            "steps": STEPS,
            "seed": SEED,
        },
    )
    elapsed = time.perf_counter() - start
    resp.raise_for_status()
    b64 = resp.json()["data"][0]["b64_json"]
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64))
    TIMINGS["generation"] = elapsed
    print(f"generation time: {elapsed:.1f}s ({elapsed / max(STEPS, 1):.1f}s/step)")
    print(f"saved {len(b64) // 4} bytes to {out_path}")


async def edit_image(client: httpx.AsyncClient, out_path: str) -> None:
    print(f"\n=== POST /v1/images/edits (prompt={EDIT_PROMPT!r}) ===")
    with open(GEN_OUT, "rb") as f:
        file_bytes = f.read()
    start = time.perf_counter()
    resp = await client.post(
        f"{FLUX_HOST}/v1/images/edits",
        files={"image": ("generated.png", file_bytes, "image/png")},
        data={
            "prompt": EDIT_PROMPT,
            "n": "1",
            "response_format": "b64_json",
            "steps": str(STEPS),
            "seed": str(SEED),
        },
    )
    elapsed = time.perf_counter() - start
    resp.raise_for_status()
    b64 = resp.json()["data"][0]["b64_json"]
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64))
    TIMINGS["edit"] = elapsed
    print(f"edit time:       {elapsed:.1f}s ({elapsed / max(STEPS, 1):.1f}s/step)")
    print(f"saved {len(b64) // 4} bytes to {out_path}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            await check_health(client)
            await check_models(client)
            await generate_image(client, GEN_OUT)
            await edit_image(client, EDIT_OUT)
        except httpx.HTTPStatusError as exc:
            print(f"\nFAILED: HTTP {exc.response.status_code}: {exc.response.text[:500]}")
            raise SystemExit(1)
        except httpx.HTTPError as exc:
            print(f"\nFAILED: connection error: {exc}")
            raise SystemExit(1)
    print("\n=== Timings ===")
    for name, secs in TIMINGS.items():
        print(f"  {name:12s} {secs:7.1f}s  ({secs / max(STEPS, 1):.1f}s/step @ {STEPS} steps, {SIZE})")
    print("\nAll checks passed. Numbers above -> "
          "deploy/dgx-spark/flux2-klein/README.md table.")


if __name__ == "__main__":
    asyncio.run(main())
