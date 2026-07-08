"""Send a single per-run analytics event to the collector service.

Design constraints (mirrors how MCP init is treated in ``main.py``):

* **Opt-in.** Disabled unless ``COLLECTOR_ENABLED`` is truthy. In GitLab CI the
  reviewer is a one-shot container that may not be able to reach a collector, so
  the default is off; a deployment that runs a collector turns it on explicitly.
* **Non-fatal.** Any failure (collector down, timeout, bad response) is caught
  and logged; it never affects the review's exit code.
* **Awaited, not fire-and-forget.** ``run_with_cleanup`` tears down the event
  loop immediately after ``process_mr`` returns, so a detached ``create_task``
  would be cancelled mid-flight. This POST happens once per run with a short
  timeout, so awaiting it costs negligible latency and actually completes.
"""

import os

import httpx

from dto.analytics import RunMetrics


def _is_enabled() -> bool:
    return os.getenv("COLLECTOR_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _timeout_seconds() -> float:
    try:
        return float(os.getenv("COLLECTOR_TIMEOUT", "3"))
    except ValueError:
        return 3.0


async def send_run_analytics(metrics: RunMetrics) -> None:
    """POST ``metrics`` to the collector. Best-effort; never raises."""
    if not _is_enabled():
        return

    base_url = os.getenv("COLLECTOR_URL", "http://collector:8000").rstrip("/")
    endpoint = f"{base_url}/api/events"

    headers = {"Content-Type": "application/json"}
    token = os.getenv("COLLECTOR_TOKEN")
    if token:
        # Optional shared-secret so the collector can reject unknown senders when
        # it is exposed beyond a trusted compose network.
        headers["X-Collector-Token"] = token

    try:
        async with httpx.AsyncClient(timeout=_timeout_seconds()) as client:
            response = await client.post(
                endpoint,
                json=metrics.model_dump(),
                headers=headers,
            )
        if response.status_code >= 400:
            print(
                f"📉 Analytics: collector returned {response.status_code} "
                f"for run {metrics.run_id} (ignored)"
            )
        else:
            print(f"📈 Analytics: sent run {metrics.run_id} to collector")
    except Exception as e:  # noqa: BLE001 — analytics must never break a review
        print(f"📉 Analytics: failed to send run {metrics.run_id} ({e}); ignored")
