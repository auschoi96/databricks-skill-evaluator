"""LLM infrastructure for skill evaluation.

Extracted from ai-dev-kit/.test/src/skill_test/optimize/judges.py

Provides model fallback chains, rate limiting, LLM call budgeting,
AI Gateway routing, and the ``completion_with_fallback`` utility used
by ``semantic_grader.py`` and ``runner.py``.

Model fallback:
    On rate limit errors (REQUEST_LIMIT_EXCEEDED), automatically retries with
    fallback models. Configure via ``GEPA_FALLBACK_MODELS`` env var (comma-separated)
    or use the built-in Databricks fallback chain.

AI Gateway support:
    Set ``DATABRICKS_AI_GATEWAY_URL`` to route calls through Databricks AI Gateway.
    Example: https://1444828305810485.ai-gateway.cloud.databricks.com/mlflow/v1
    Works alongside the standard serving endpoint approach.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_LM = os.environ.get("GEPA_JUDGE_LM", "databricks:/databricks-claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# Fallback model chain for rate limit errors
# ---------------------------------------------------------------------------

_DEFAULT_FALLBACK_MODELS = [
    "databricks/databricks-gpt-5-2",
    "databricks/databricks-gemini-3-1-pro",
    "databricks/databricks-claude-opus-4-5",
    "databricks/databricks-gpt-5",
    "databricks/databricks-claude-sonnet-4-6",
    "databricks/databricks-claude-sonnet-4-5",
]


def _get_fallback_models() -> list[str]:
    """Get fallback model chain from env or defaults."""
    custom = os.environ.get("GEPA_FALLBACK_MODELS", "")
    if custom.strip():
        return [m.strip() for m in custom.split(",") if m.strip()]
    return list(_DEFAULT_FALLBACK_MODELS)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Check if an exception is a rate limit / request limit exceeded error."""
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in [
            "rate_limit",
            "rate limit",
            "request_limit_exceeded",
            "request limit exceeded",
            "too many requests",
            "429",
            "token.*per.*minute",
        ]
    )


def _is_workspace_error(exc: Exception) -> bool:
    """Detect workspace-level errors where retrying or falling back is pointless.

    Catches 403/IP ACL blocks, auth failures, and network errors that indicate
    the entire workspace is unreachable — not just a single model rate limit.
    """
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in [
            "403",
            "forbidden",
            "ip access list",
            "ip acl",
            "not on the ip access list",
            "unauthorized",
            "401",
            "authentication failed",
            "invalid token",
            "could not resolve host",
            "connection refused",
            "connection error",
            "network is unreachable",
            "name or service not known",
            "no such host",
            "token refresh",
        ]
    )


# ---------------------------------------------------------------------------
# Global LLM call budget
# ---------------------------------------------------------------------------


class _LLMCallBudget:
    """Thread-safe counter that enforces a global cap on LLM API calls.

    Configurable via GEPA_MAX_LLM_CALLS env var.  When unset or 0 the budget
    is unlimited.
    """

    def __init__(self):
        import threading as _threading

        self._lock = _threading.Lock()
        self._count = 0
        max_str = os.environ.get("GEPA_MAX_LLM_CALLS", "0")
        try:
            self._max = int(max_str)
        except ValueError:
            self._max = 0

    @property
    def max_calls(self) -> int:
        return self._max

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def acquire(self) -> bool:
        """Increment counter. Returns False if budget exhausted."""
        with self._lock:
            if self._max > 0 and self._count >= self._max:
                return False
            self._count += 1
            return True

    def exhausted(self) -> bool:
        with self._lock:
            return self._max > 0 and self._count >= self._max


_llm_budget = _LLMCallBudget()


# ---------------------------------------------------------------------------
# AI Gateway support
# ---------------------------------------------------------------------------


def _get_gateway_base_url() -> str | None:
    """Return the AI Gateway base URL if configured, else None.

    Reads os.environ at call time (not import time) so that env vars
    set by runner.py's early config loading are picked up before judges
    are created.

    Strips common API path suffixes (e.g. ``/chat/completions``) that users
    might include by mistake — litellm appends its own path to the base URL.
    """
    url = os.environ.get("DATABRICKS_AI_GATEWAY_URL", "").strip()
    if not url:
        return None
    url = url.rstrip("/")
    # Strip API path suffixes users might include by mistake
    for suffix in ("/chat/completions", "/completions", "/embeddings"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")


def _to_litellm_model(model: str) -> tuple[str, str | None, str | None]:
    """Convert a model string to (litellm_model, base_url, api_key) for completion calls.

    If AI Gateway is configured and model is a databricks/ model, routes
    through the gateway as an OpenAI-compatible endpoint.  The OpenAI
    provider in litellm does not auto-read ``DATABRICKS_TOKEN``, so we
    pass it explicitly as ``api_key``.

    Returns:
        (model_string, base_url_or_None, api_key_or_None)
    """
    gateway = _get_gateway_base_url()
    if gateway and model.startswith("databricks/"):
        # Route through AI Gateway as OpenAI-compatible endpoint
        endpoint_name = model.split("/", 1)[1]
        api_key = os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_API_KEY", "")
        return f"openai/{endpoint_name}", gateway, api_key or None
    return model, None, None


# ---------------------------------------------------------------------------
# Completion with fallback
# ---------------------------------------------------------------------------


def completion_with_fallback(*, model: str, max_retries: int = 3, **kwargs) -> Any:
    """Call litellm.completion with model fallback on rate limit errors.

    Tries the primary model first. On rate limit errors, cycles through
    the fallback chain. Each model gets ``max_retries`` attempts with
    exponential backoff before moving to the next.

    Workspace-level errors (403/IP ACL/auth) are raised immediately —
    fallback models hit the same blocked workspace and would all fail.

    Respects the global LLM call budget (``GEPA_MAX_LLM_CALLS``).

    Also supports AI Gateway: if DATABRICKS_AI_GATEWAY_URL is set,
    databricks/ models are routed through the gateway.
    """
    import litellm

    if not _llm_budget.acquire():
        raise RuntimeError(
            f"GEPA LLM call budget exhausted ({_llm_budget.max_calls} calls). "
            "Set GEPA_MAX_LLM_CALLS to increase or unset to disable."
        )

    models_to_try = [model] + [m for m in _get_fallback_models() if m != model]

    last_err: Exception | None = None
    for model_str in models_to_try:
        litellm_model, base_url, api_key = _to_litellm_model(model_str)

        call_kwargs = dict(kwargs)
        call_kwargs["model"] = litellm_model
        if base_url:
            call_kwargs["base_url"] = base_url
        if api_key:
            call_kwargs["api_key"] = api_key

        for attempt in range(max_retries):
            if attempt > 0:
                delay = min(2**attempt, 30)
                time.sleep(delay)
            try:
                return litellm.completion(**call_kwargs)
            except Exception as e:
                last_err = e
                # Workspace-level errors: fail fast, no fallback
                if _is_workspace_error(e):
                    logger.error(
                        "Workspace error (fail-fast): %s — not trying fallback models",
                        e,
                    )
                    raise
                if _is_rate_limit_error(e):
                    if attempt == max_retries - 1:
                        logger.warning(
                            "Model '%s' rate limited after %d attempts, trying next fallback",
                            model_str,
                            max_retries,
                        )
                    continue
                # Non-rate-limit error: don't retry, try next model
                logger.warning("Model '%s' failed (non-rate-limit): %s", model_str, e)
                break

    raise last_err  # type: ignore[misc]
