#!/usr/bin/env python3
"""
Verify the Langfuse tracing setup before running a real review.

Checks, in order: the SDK is installed, the env vars are set, the host answers
with the configured keys, and a trace built through the reviewer's *own* config
plumbing actually lands — including nesting and session grouping. It uses a fake
chat model, so no OpenRouter call is made and nothing is billed.

    python scripts/test_langfuse.py

Exits 0 when a trace was written and read back, non-zero otherwise.
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = REPO_ROOT / "reviewer"
# Import the reviewer's real tracing helpers rather than reimplementing them —
# the point of this script is to test the wiring the review actually uses.
sys.path.insert(0, str(REVIEWER))
load_dotenv(REPO_ROOT / ".env")

from core.config.tracing import (  # noqa: E402
    build_thread_id,
    build_run_config,
    merge_thread_into_config,
)
from core.config.langfuse_config import (  # noqa: E402
    flush_langfuse,
    get_langfuse_base_url,
    get_langfuse_handler,
    langfuse_enabled,
)

# A recognizable, obviously-fake MR so the test trace can't be mistaken for a
# real review in the Langfuse UI.
THREAD_ID = build_thread_id("langfuse-selftest/project", 0)


def check_env() -> bool:
    """Report what's configured before touching the network."""
    print("1. Configuration")
    base_url = get_langfuse_base_url()
    ok = bool(base_url)
    if base_url:
        print(f"   ✓ LANGFUSE_BASE_URL: {base_url}")
    else:
        print("   ✗ LANGFUSE_BASE_URL: not set (this is also the on/off switch)")
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        value = os.getenv(var, "").strip()
        if value:
            print(f"   ✓ {var}: {value[:8]}…")
        else:
            print(f"   ✗ {var}: not set")
            ok = False
    print(f"   • enabled: {langfuse_enabled()}")
    if not ok:
        print(
            "\n   Set all three in .env: LANGFUSE_BASE_URL (e.g. http://localhost:3000\n"
            "   self-hosted, or https://cloud.langfuse.com) plus both keys from the\n"
            "   Langfuse UI → Settings → API Keys."
        )
    return ok


async def emit_trace() -> bool:
    """Run a nested chain through build_run_config, exactly like a file review."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import RunnableConfig, RunnableLambda

    print("\n2. Connection")
    handler = get_langfuse_handler()
    if handler is None:
        print("   ✗ No callback handler — see the warning above.")
        return False

    async def reviewer_node(_input, config: RunnableConfig = None):
        """Stands in for a graph reviewer node: derives a child config, calls the LLM."""
        chain = ChatPromptTemplate.from_messages(
            [("system", "You are the service reviewer."), ("human", "{input}")]
        ) | FakeListChatModel(responses=['{"issues": []}'])
        return (
            await chain.ainvoke(
                {"input": "=== File Content ===\n1|package main\n"},
                config=merge_thread_into_config(config, run_name="service_review"),
            )
        ).content

    print("\n3. Emitting a test trace")
    config = build_run_config(
        THREAD_ID,
        run_name="langfuse-selftest",
        extra_metadata={"file_path": "selftest.go", "mr_id": 0},
        tags=["langfuse-selftest"],
    )
    if "callbacks" not in config:
        print("   ✗ Handler was not attached to the run config.")
        return False
    await RunnableLambda(reviewer_node, name="review_workflow").ainvoke({}, config=config)
    flush_langfuse()
    print("   ✓ Trace emitted and flushed")
    return True


async def read_back() -> bool:
    """Poll the API until the trace is queryable (ingestion is asynchronous)."""
    from langfuse import get_client

    print("\n4. Reading it back (server-side ingestion takes ~15-30s)")
    client = get_client()
    for attempt in range(1, 21):
        await asyncio.sleep(2)
        try:
            traces = client.api.trace.list(session_id=THREAD_ID, limit=1)
        except Exception as e:
            print(f"   ✗ Query failed: {e}")
            return False
        if traces.data:
            trace = client.api.trace.get(traces.data[0].id)
            print(f"   ✓ Trace {trace.id}")
            print(f"     name={trace.name!r} session={trace.session_id!r} release={trace.release!r}")
            nested = sum(1 for o in trace.observations if o.parent_observation_id)
            print(f"     {len(trace.observations)} observations ({nested} nested)")
            generations = [o for o in trace.observations if o.type == "GENERATION"]
            if generations and generations[0].input:
                print("     prompt captured ✓ — this is the context view you're after")
            elif not generations:
                print("     ⚠️  no GENERATION observation — prompts would not be captured")
                return False
            print(f"\n   Open {get_langfuse_base_url()} → Tracing → Sessions → {THREAD_ID}")
            return True
        print(f"   … not ingested yet ({attempt}/20)")
    print("   ✗ The trace never appeared. Flush succeeded, so check the Langfuse")
    print("     project the keys belong to, and whether a proxy is intercepting the export.")
    return False


async def main() -> int:
    print("=" * 62)
    print("Langfuse setup check")
    print("=" * 62 + "\n")
    if not check_env():
        return 1
    if not await emit_trace():
        return 1
    if not await read_back():
        return 1
    print("\n✅ Langfuse is wired up correctly.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
