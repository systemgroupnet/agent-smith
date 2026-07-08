"""Run analytics: emit one event per review to the collector service."""

from core.analytics.client import send_run_analytics

__all__ = ["send_run_analytics"]
