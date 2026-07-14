"""
Review-Effort Estimator

A parallel, metadata-only pass that estimates how hard a merge request is for a
HUMAN reviewer (1 = trivial .. 5 = very hard). It runs concurrently with the file
reviews (it needs only MR metadata, not the review results), sends the model
lightweight signals built by ``core/utils/effort_insight.py`` — per-file change
type and line counts, the directory tree/relations implied by the paths, the
language mix, and the module spread — and never the actual diffs. Non-fatal: any
failure returns None and the review proceeds unlabeled.
"""

import json
from typing import List, Optional

from langchain_core.runnables import RunnableConfig

from core.agents.base_agent import StructuredOutputAgent
from core.prompts import get_effort_estimator_prompt
from core.utils.effort_insight import build_effort_insight
from dto.general import EffortEstimate


async def estimate_review_effort(
    changes: List[dict], config: Optional[RunnableConfig] = None
) -> Optional[int]:
    """Estimate MR review effort (1..5). Returns None on failure (non-fatal)."""
    if not changes:
        return None
    try:
        insight = build_effort_insight(changes)
        agent = StructuredOutputAgent(
            system_prompt=get_effort_estimator_prompt(),
            output_model=EffortEstimate,
            temperature=0.0,
        )
        payload = json.dumps(insight, ensure_ascii=False, indent=2)
        result = await agent.run(payload, config=config)
        print(f"  🧮 Review effort estimated: {result.effort}/5 — {result.reasoning[:140]}")
        return result.effort
    except Exception as e:
        print(f"  ⚠️  Review-effort estimation failed (non-fatal): {e}")
        return None
