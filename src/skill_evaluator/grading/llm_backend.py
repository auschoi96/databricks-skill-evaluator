"""LLM infrastructure for skill evaluation.

Provides model fallback chains, rate limiting, LLM call budgeting,
AI Gateway routing, and the ``completion_with_fallback`` utility used
by ``semantic_grader.py`` and evaluation levels.

Uses the OpenAI client to call Databricks Foundation Model APIs
(OpenAI-compatible serving endpoints).

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

DEFAULT_JUDGE_LM = os.environ.get("GEPA_JUDGE_LM", "databricks/databricks-claude-opus-4-6")

# ---------------------------------------------------------------------------
# Fallback model chain for rate limit errors
# ---------------------------------------------------------------------------

_DEFAULT_FALLBACK_MODELS = [
    "databricks/databricks-claude-opus-4-6",
    "databricks/databricks-claude-sonnet-4-6",
    "databricks/databricks-claude-opus-4-5",
    "databricks/databricks-claude-sonnet-4-5",
    "databricks/databricks-gpt-5-2",
    "databricks/databricks-gemini-3-1-pro",
    "databricks/databricks-gpt-5",
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

    Catches 403/IP ACL blocks and auth failures that indicate the workspace
    is permanently unreachable — not just a transient network hiccup.

    Connection/network errors are NOT treated as workspace errors because they
    can be transient and should be retried with backoff.
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
            "token refresh",
        ]
    )


def _is_transient_error(exc: Exception) -> bool:
    """Detect transient network/connection errors that should be retried."""
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in [
            "could not resolve host",
            "connection refused",
            "connection error",
            "network is unreachable",
            "name or service not known",
            "no such host",
            "connection reset",
            "timeout",
            "timed out",
            "temporary failure",
            "eof occurred",
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
    might include by mistake.
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


# ---------------------------------------------------------------------------
# OpenAI client for Databricks Foundation Model APIs
# ---------------------------------------------------------------------------


_cached_workspace_client = None
_cached_workspace_profile = None


def _get_credentials_from_sdk() -> tuple[str, str]:
    """Get host and token from Databricks SDK using saved DSE config or default profile.

    Returns:
        (host, token) — host is the workspace URL, token is a fresh OAuth/PAT token.

    The WorkspaceClient is cached so that its internal token-refresh logic
    stays alive across calls.  Each invocation calls ``config.authenticate()``
    to obtain a *fresh* bearer token, avoiding 403 errors when OAuth tokens
    expire during long-running evaluations (~90 min).
    """
    global _cached_workspace_client, _cached_workspace_profile

    # Try loading profile from saved DSE config first
    profile = None
    try:
        from ..auth import load_config
        ws_config = load_config()
        if ws_config and ws_config.host:
            profile = ws_config.profile
    except Exception:
        pass

    try:
        from databricks.sdk import WorkspaceClient

        # Reuse the WorkspaceClient so its internal OAuth state (refresh
        # tokens, etc.) persists.  Recreate only if the profile changed.
        if _cached_workspace_client is None or _cached_workspace_profile != profile:
            _cached_workspace_client = (
                WorkspaceClient(profile=profile) if profile else WorkspaceClient()
            )
            _cached_workspace_profile = profile

        w = _cached_workspace_client
        host = w.config.host.rstrip("/")

        # Always call authenticate() to get a fresh token.  For PAT auth
        # this is a no-op (returns the same token).  For OAuth/U2M auth
        # this refreshes the token when it's near expiry.
        token = w.config.token
        if not token:
            headers = w.config.authenticate()
            auth_header = headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer "):]
        logger.info("LLM backend: credentials from Databricks SDK (profile=%s)", profile or "default")
        return host, token
    except Exception as e:
        raise RuntimeError(
            f"Cannot resolve Databricks credentials. Set DATABRICKS_HOST + DATABRICKS_TOKEN "
            f"env vars, or configure a profile in ~/.databrickscfg. SDK error: {e}"
        ) from e


def _get_openai_client_and_model(model: str) -> tuple[Any, str]:
    """Build an OpenAI client configured for the given model string.

    Supports two routing modes:
    1. AI Gateway: if DATABRICKS_AI_GATEWAY_URL is set, uses that as base_url.
    2. Direct: constructs base_url from DATABRICKS_HOST or DATABRICKS_API_BASE,
       appending /serving-endpoints if needed.

    Falls back to the Databricks SDK (WorkspaceClient) for host and token
    when environment variables are not set (e.g., when running inside the
    MCP server process).

    Args:
        model: Model string like "databricks/databricks-claude-sonnet-4-6"

    Returns:
        (client, endpoint_name) where endpoint_name is the serving endpoint name.
    """
    from openai import OpenAI

    # Extract endpoint name from "databricks/<endpoint>" format
    if model.startswith("databricks/"):
        endpoint_name = model.split("/", 1)[1]
    else:
        endpoint_name = model

    # Resolve credentials: env vars first, then Databricks SDK (OAuth/U2M)
    api_key = (
        os.environ.get("DATABRICKS_TOKEN")
        or os.environ.get("DATABRICKS_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")

    if not api_key or not host:
        sdk_host, sdk_token = _get_credentials_from_sdk()
        if not host:
            host = sdk_host
        if not api_key:
            api_key = sdk_token

    # Determine base_url
    gateway = _get_gateway_base_url()
    if gateway:
        base_url = gateway
    else:
        api_base = os.environ.get("DATABRICKS_API_BASE", "").rstrip("/")
        if api_base:
            base_url = api_base
        else:
            base_url = f"{host}/serving-endpoints"

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=180)
    return client, endpoint_name


# ---------------------------------------------------------------------------
# Completion with fallback
# ---------------------------------------------------------------------------


def completion_with_fallback(*, model: str, max_retries: int = 3, **kwargs) -> Any:
    """Call Databricks Foundation Model API with model fallback on rate limit errors.

    Uses the OpenAI client pointed at Databricks serving endpoints.

    Tries the primary model first. On rate limit errors, cycles through
    the fallback chain. Each model gets ``max_retries`` attempts with
    exponential backoff before moving to the next.

    Workspace-level errors (403/IP ACL/auth) are raised immediately —
    fallback models hit the same blocked workspace and would all fail.

    Respects the global LLM call budget (``GEPA_MAX_LLM_CALLS``).

    Also supports AI Gateway: if DATABRICKS_AI_GATEWAY_URL is set,
    databricks/ models are routed through the gateway.
    """
    if not _llm_budget.acquire():
        raise RuntimeError(
            f"GEPA LLM call budget exhausted ({_llm_budget.max_calls} calls). "
            "Set GEPA_MAX_LLM_CALLS to increase or unset to disable."
        )

    models_to_try = [model] + [m for m in _get_fallback_models() if m != model]

    last_err: Exception | None = None
    for model_str in models_to_try:
        client, endpoint_name = _get_openai_client_and_model(model_str)

        auth_retried = False
        for attempt in range(max_retries):
            if attempt > 0:
                delay = min(2**attempt, 30)
                time.sleep(delay)
            try:
                return client.chat.completions.create(
                    model=endpoint_name,
                    **kwargs,
                )
            except Exception as e:
                last_err = e
                # Workspace-level errors (403/auth): retry once with a
                # fresh token in case the OAuth token expired mid-eval.
                if _is_workspace_error(e):
                    if not auth_retried:
                        auth_retried = True
                        logger.warning(
                            "Workspace auth error — refreshing credentials and retrying: %s", e,
                        )
                        client, endpoint_name = _get_openai_client_and_model(model_str)
                        continue
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
                # Transient connection errors: retry with backoff
                if _is_transient_error(e):
                    logger.warning(
                        "Model '%s' transient error (attempt %d/%d): %s",
                        model_str, attempt + 1, max_retries, e,
                    )
                    continue
                # Non-retryable error: don't retry, try next model
                logger.warning("Model '%s' failed (non-retryable): %s", model_str, e)
                break

    raise last_err  # type: ignore[misc]
