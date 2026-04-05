"""Claude Agent SDK executor for agent-based skill evaluation.

Extracted from ai-dev-kit/.test/src/skill_test/agent/executor.py

Wraps claude_agent_sdk.query() to run a real Claude Code instance with
a candidate SKILL.md injected as system prompt, captures streaming events,
and builds TraceMetrics from the session.

Usage:
    result = await run_agent(
        prompt="Create a metric view for order analytics",
        skill_md="# Metric Views\n...",
        mcp_config={"databricks": databricks_server},
    )
    print(result.response_text)
    print(result.trace_metrics.to_dict())
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.trace_models import FileOperation, ToolCall, TraceMetrics, TokenUsage

logger = logging.getLogger(__name__)

_mlflow_env_lock = threading.Lock()
_mlflow_env_configured = False

# Serialize process_transcript calls across parallel agents to avoid
# burst HTTP load on the MLflow tracking server when multiple agents
# finish concurrently (e.g. --parallel-agents 3).
# Lazy per-loop factory: asyncio.Semaphore binds to the running loop at
# creation time. When _run_in_fresh_loop creates a new loop the module-level
# semaphore would crash with "attached to a different loop". Instead we
# cache one semaphore per event-loop id.
_transcript_semaphores: dict[int, asyncio.Semaphore] = {}
_transcript_semaphore_lock = threading.Lock()


def _get_transcript_semaphore() -> asyncio.Semaphore:
    """Return a Semaphore(1) bound to the current running event loop."""
    loop_id = id(asyncio.get_running_loop())
    with _transcript_semaphore_lock:
        if loop_id not in _transcript_semaphores:
            _transcript_semaphores[loop_id] = asyncio.Semaphore(1)
        return _transcript_semaphores[loop_id]


@dataclass
class AgentEvent:
    """A captured event from the agent execution stream."""

    type: str  # tool_use, tool_result, text, result, system, error
    timestamp: datetime
    data: dict[str, Any]


@dataclass
class AgentResult:
    """Result of a single agent execution.

    Contains the final response text, trace metrics built from captured
    events, and the raw event stream for detailed analysis.
    """

    response_text: str
    trace_metrics: TraceMetrics
    events: list[AgentEvent] = field(default_factory=list)
    session_id: str | None = None
    duration_ms: int | None = None
    success: bool = True
    error: str | None = None
    mlflow_trace_id: str | None = None


def _build_trace_metrics(
    events: list[AgentEvent],
    session_id: str,
) -> TraceMetrics:
    """Build TraceMetrics from captured agent events.

    Maps the SDK streaming events back to the same TraceMetrics model
    used by the JSONL transcript parser, enabling reuse of all existing
    trace scorers.
    """
    metrics = TraceMetrics(session_id=session_id)

    tool_calls_by_id: dict[str, ToolCall] = {}
    total_input = 0
    total_output = 0
    total_cache_creation = 0
    total_cache_read = 0
    num_turns = 0
    num_user_messages = 1  # The initial prompt counts as one

    for event in events:
        ts = event.timestamp

        if metrics.start_time is None:
            metrics.start_time = ts

        if event.type == "tool_use":
            _raw_input = event.data.get("input", {})
            tc = ToolCall(
                id=event.data.get("id", str(uuid.uuid4())),
                name=event.data.get("name", "unknown"),
                input=_raw_input if isinstance(_raw_input, dict) else {},
                timestamp=ts,
            )
            tool_calls_by_id[tc.id] = tc
            metrics.tool_calls.append(tc)

        elif event.type == "tool_result":
            tool_use_id = event.data.get("tool_use_id", "")
            result_text = event.data.get("content", "")
            is_error = event.data.get("is_error", False)

            if tool_use_id in tool_calls_by_id:
                tc = tool_calls_by_id[tool_use_id]
                tc.result = result_text if isinstance(result_text, str) else str(result_text)
                tc.success = not is_error

                # Extract file operations from tool results
                tool_name = tc.name
                tool_input = tc.input
                if tool_name == "Write" and tc.success:
                    fp = tool_input.get("file_path", "")
                    if fp:
                        metrics.files_created.append(fp)
                        metrics.file_operations.append(FileOperation(type="create", file_path=fp, timestamp=ts))
                elif tool_name == "Edit" and tc.success:
                    fp = tool_input.get("file_path", "")
                    if fp:
                        metrics.files_modified.append(fp)
                        metrics.file_operations.append(FileOperation(type="edit", file_path=fp, timestamp=ts))
                elif tool_name == "Read":
                    fp = tool_input.get("file_path", "")
                    if fp:
                        metrics.files_read.append(fp)
                        metrics.file_operations.append(FileOperation(type="read", file_path=fp, timestamp=ts))

        elif event.type == "assistant_turn":
            num_turns += 1
            usage = event.data.get("usage", {})
            if usage:
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
                total_cache_creation += usage.get("cache_creation_input_tokens", 0)
                total_cache_read += usage.get("cache_read_input_tokens", 0)

        elif event.type == "result":
            metrics.end_time = ts

    # If no explicit end time, use last event timestamp
    if metrics.end_time is None and events:
        metrics.end_time = events[-1].timestamp

    # Aggregate tool counts
    for tc in metrics.tool_calls:
        metrics.tool_counts[tc.name] = metrics.tool_counts.get(tc.name, 0) + 1
        cat = tc.tool_category
        metrics.tool_category_counts[cat] = metrics.tool_category_counts.get(cat, 0) + 1

    metrics.total_tool_calls = len(metrics.tool_calls)
    metrics.total_input_tokens = total_input
    metrics.total_output_tokens = total_output
    metrics.total_cache_creation_tokens = total_cache_creation
    metrics.total_cache_read_tokens = total_cache_read
    metrics.num_turns = num_turns
    metrics.num_user_messages = num_user_messages

    return metrics


def _load_mcp_config(project_root: Path | None = None) -> dict[str, Any]:
    """Load MCP server config from .mcp.json, resolving variable references.

    Resolves ``${CLAUDE_PLUGIN_ROOT}`` to the project root and strips
    ``defer_loading`` (not relevant for the agent SDK).  If the configured
    Python interpreter does not exist on disk, falls back to ``sys.executable``
    so tests work across conda / venv / system-python setups.
    """
    import json
    import shutil
    import sys

    repo_root = project_root if project_root is not None else Path.cwd()
    mcp_json = repo_root / ".mcp.json"
    if not mcp_json.exists():
        return {}

    try:
        data = json.loads(mcp_json.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    servers = data.get("mcpServers", {})
    resolved: dict[str, Any] = {}
    for name, cfg in servers.items():
        resolved_cfg: dict[str, Any] = {}
        for key, val in cfg.items():
            if key == "defer_loading":
                continue  # Not relevant for agent SDK
            if isinstance(val, str):
                resolved_cfg[key] = (
                    val.replace("${CLAUDE_PLUGIN_ROOT}", str(repo_root))
                       .replace("${CLAUDE_PROJECT_ROOT}", str(repo_root))
                )
            elif isinstance(val, list):
                resolved_cfg[key] = [
                    v.replace("${CLAUDE_PLUGIN_ROOT}", str(repo_root))
                     .replace("${CLAUDE_PROJECT_ROOT}", str(repo_root))
                    if isinstance(v, str) else v for v in val
                ]
            else:
                resolved_cfg[key] = val

        # Fall back to current Python if the configured interpreter is missing.
        # This handles .mcp.json referencing .venv/bin/python when the env uses
        # conda, system python, or a differently-named venv.
        cmd = resolved_cfg.get("command", "")
        if cmd and not Path(cmd).exists() and not shutil.which(cmd):
            original = cmd
            resolved_cfg["command"] = sys.executable
            logger.warning(
                "MCP server '%s': configured python '%s' not found, falling back to '%s'",
                name, original, sys.executable,
            )

        if resolved_cfg:
            resolved[name] = resolved_cfg
    return resolved


def _discover_mcp_tool_names(
    mcp_config: dict[str, Any],
    tool_modules: list[str] | None = None,
) -> list[str]:
    """Discover MCP tool names by statically parsing MCP server source code.

    Claude Code names MCP tools as ``mcp__<server>__<tool>``.  Uses
    AST-based extraction from :mod:`mcp_resolver` so we don't need to
    import (and thus *start*) the full MCP server just to enumerate
    tool names.

    Falls back to an empty list if extraction fails.
    """
    from ..mcp_resolver import MCPConfig

    cfg = MCPConfig(servers=mcp_config)
    cfg.resolve_available_tools()

    tool_names = cfg.available_tools
    logger.info("Discovered %d MCP tools from %d servers", len(tool_names), len(mcp_config))
    return tool_names


_ENV_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "DATABRICKS_",
    "MLFLOW_",
)


def _resolve_env_refs(value: str) -> str:
    """Expand ${VAR} references in a settings value using os.environ.

    Supports ${VAR} and ${VAR:-default} syntax. Unresolved refs with no
    default are left as-is.
    """
    import re

    def _replacer(m: re.Match) -> str:
        var = m.group(1)
        if ":-" in var:
            name, default = var.split(":-", 1)
            return os.environ.get(name, default)
        return os.environ.get(var, m.group(0))

    return re.sub(r"\$\{([^}]+)\}", _replacer, value)


def _get_agent_env(
    project_root: Path | None = None,
    settings_path: Path | None = None,
) -> dict[str, str]:
    """Build environment variables for the Claude agent subprocess.

    Loads Databricks FMAPI configuration from a settings file (same pattern
    as databricks-builder-app), with env var overrides on top.

    Args:
        project_root: Project root directory. If None, uses Path.cwd().
        settings_path: Explicit path to a settings JSON file. If provided,
            this is used instead of the default search order.

    Settings file search order (when settings_path is None):
        1. <project_root>/claude_agent_settings.json
        2. <project_root>/.claude/agent_settings.json

    Expected format (same as builder app):
        {
            "env": {
                "ANTHROPIC_MODEL": "databricks-claude-opus-4-6",
                "ANTHROPIC_BASE_URL": "https://<host>/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "${DATABRICKS_TOKEN}",
                "DATABRICKS_CONFIG_PROFILE": "e2-demo-field-eng",
                ...
            }
        }

    Values support ${VAR} and ${VAR:-default} interpolation from env vars.
    Environment variables with matching prefixes override settings file values.
    """
    import json

    env: dict[str, str] = {}

    repo_root = project_root if project_root is not None else Path.cwd()

    # 1. Load from settings file (if exists)
    if settings_path is not None:
        search_paths = [settings_path]
    else:
        search_paths = [
            repo_root / "claude_agent_settings.json",
            repo_root / ".claude" / "agent_settings.json",
        ]
    for p in search_paths:
        if p.exists():
            try:
                settings = json.loads(p.read_text())
                file_env = settings.get("env", {})
                for k, v in file_env.items():
                    if isinstance(v, str):
                        env[k] = _resolve_env_refs(v)
                logger.info("Loaded agent env from %s (%d vars)", p, len(file_env))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load %s: %s", p, e)
            break  # use first found

    # 2. Env vars with known prefixes override settings file values
    # Skip internal Claude Code vars that would confuse the subprocess
    _skip_keys = {
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
    }
    for key, value in os.environ.items():
        if key in _skip_keys:
            continue
        if any(key.startswith(p) for p in _ENV_PREFIXES) and value:
            env[key] = value

    # Remove internal Claude Code vars that may have leaked from settings file
    for k in _skip_keys:
        env.pop(k, None)

    # 3. Fall back to Databricks SDK when host/token are missing (e.g. MCP server process)
    if not env.get("DATABRICKS_HOST") or not env.get("DATABRICKS_TOKEN"):
        try:
            from ..auth import load_config
            profile = None
            ws_config = load_config()
            if ws_config and ws_config.host:
                profile = ws_config.profile

            from databricks.sdk import WorkspaceClient
            w = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
            sdk_host = w.config.host.rstrip("/")
            # config.token is None for OAuth (databricks-cli) auth —
            # get it from the header factory which handles token refresh.
            sdk_token = w.config.token
            if not sdk_token:
                headers = w.config.authenticate()
                auth_header = headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    sdk_token = auth_header[len("Bearer "):]

            env.setdefault("DATABRICKS_HOST", sdk_host)
            env.setdefault("DATABRICKS_TOKEN", sdk_token)
            env.setdefault("ANTHROPIC_BASE_URL", f"{sdk_host}/anthropic")
            env.setdefault("ANTHROPIC_AUTH_TOKEN", sdk_token)
            if profile:
                env.setdefault("DATABRICKS_CONFIG_PROFILE", profile)
            logger.info("Agent env: credentials from Databricks SDK (profile=%s)", profile or "default")
        except Exception as e:
            logger.warning("Could not resolve Databricks credentials from SDK: %s", e)

    # 4. Ensure required defaults
    env.setdefault("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1")
    env.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "600000")  # 10 min

    return env


def _get_mlflow_stop_hook(
    mlflow_experiment: str | None = None,
    skill_name: str | None = None,
    project_root: Path | None = None,
    settings_path: Path | None = None,
):
    """Create an MLflow Stop hook that processes the transcript into a real trace.

    Mirrors the pattern from databricks-builder-app/server/services/agent.py:
    - MLflow tracking URI and experiment are set at hook CREATION time
    - The hook itself just calls setup_mlflow() then process_transcript()
    - No conditional gates -- configure every time for reliability

    Returns hook_fn or None if MLflow is not available.
    """
    try:
        from mlflow.claude_code.tracing import process_transcript, setup_mlflow
        import mlflow
    except ImportError:
        logger.warning(
            "mlflow.claude_code.tracing not available -- traces will not be logged. Ensure mlflow>=3.10.1 is installed."
        )
        return None

    # One-time environment and MLflow configuration (thread-safe).
    # All os.environ writes happen here, once, to avoid races in parallel runs.
    global _mlflow_env_configured
    with _mlflow_env_lock:
        if not _mlflow_env_configured:
            # Apply DATABRICKS_* and MLFLOW_* vars from agent settings to os.environ
            # so SkillTestConfig / MLflow can pick them up for auth.
            agent_env = _get_agent_env(project_root=project_root, settings_path=settings_path)
            for key, value in agent_env.items():
                if key.startswith(("DATABRICKS_", "MLFLOW_")):
                    os.environ[key] = value

            # Configure MLflow at hook creation time (matches builder app pattern).
            from ..core.config import EvaluatorConfig as SkillTestConfig

            agent_experiment = mlflow_experiment or os.environ.get(
                "SKILL_TEST_MLFLOW_EXPERIMENT",
                "/Shared/skill-tests",
            )
            os.environ["MLFLOW_EXPERIMENT_NAME"] = agent_experiment

            stc = SkillTestConfig()
            tracking_uri = stc.mlflow.tracking_uri
            experiment_name = agent_experiment

            # Sync env vars so setup_mlflow() from mlflow.claude_code.tracing agrees
            os.environ["MLFLOW_TRACKING_URI"] = tracking_uri
            os.environ["MLFLOW_EXPERIMENT_NAME"] = experiment_name
            os.environ["MLFLOW_CLAUDE_TRACING_ENABLED"] = "true"

            mlflow.set_tracking_uri(tracking_uri)
            mlflow.set_registry_uri("databricks-uc")
            try:
                mlflow.set_experiment(experiment_name)
            except Exception as e:
                logger.warning("MLflow set_experiment('%s') failed: %s", experiment_name, e)
                try:
                    mlflow.create_experiment(experiment_name)
                    mlflow.set_experiment(experiment_name)
                except Exception:
                    logger.warning(
                        "Cannot access MLflow experiment '%s' on %s. "
                        "Traces will not be logged. Check DATABRICKS_CONFIG_PROFILE.",
                        experiment_name,
                        tracking_uri,
                    )
                    return None

            print(f"    [MLflow] Tracing configured: uri={tracking_uri} experiment={experiment_name}")
            _mlflow_env_configured = True

    async def _upload_trace_background(session_id, transcript_path):
        """Upload transcript to MLflow as a trace (best-effort).

        Judges are field-based and don't consume MLflow traces, so this is
        purely for observability logging.  Enabled by default.  The exporter
        logger is suppressed during process_transcript() so any artifact-upload
        warnings (e.g. S3 presigned-URL 403) are silent.

        Set SKILL_TEST_UPLOAD_TRACES=false to disable.
        """
        if os.environ.get("SKILL_TEST_UPLOAD_TRACES", "").lower() in ("0", "false", "no"):
            logger.debug("Trace upload disabled (SKILL_TEST_UPLOAD_TRACES=false)")
            return

        # Suppress the mlflow_v3 exporter warnings -- if the S3 presigned
        # URL fails, the WARNING is logged by MLflow internals before our
        # exception handler runs.  Raising the level prevents noisy output.
        _exporter_logger = logging.getLogger("mlflow.tracing.export.mlflow_v3")
        _prev_level = _exporter_logger.level
        _exporter_logger.setLevel(logging.ERROR)
        try:
            setup_mlflow()
            loop = asyncio.get_running_loop()
            async with _get_transcript_semaphore():
                trace = await asyncio.wait_for(
                    loop.run_in_executor(None, process_transcript, transcript_path, session_id),
                    timeout=60.0,
                )
            if trace:
                print(f"    [MLflow] Trace uploaded (background): {trace.info.trace_id}")
                try:
                    client = mlflow.MlflowClient()
                    trace_id = trace.info.trace_id
                    requested_model = os.environ.get("ANTHROPIC_MODEL", "")
                    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
                    if requested_model:
                        client.set_trace_tag(trace_id, "databricks.requested_model", requested_model)
                    if base_url:
                        client.set_trace_tag(trace_id, "databricks.model_serving_endpoint", base_url)
                    client.set_trace_tag(trace_id, "mlflow.source", "skill-test-agent-eval")
                    if skill_name:
                        client.set_trace_tag(trace_id, "skill_name", skill_name)
                except Exception as tag_err:
                    print(f"    [MLflow] Warning: could not add tags: {tag_err}")
        except asyncio.TimeoutError:
            print(f"    [MLflow] Warning: background trace upload timed out (session={session_id})")
        except Exception as e:
            print(f"    [MLflow] Warning: background trace upload failed: {e}")
        finally:
            _exporter_logger.setLevel(_prev_level)

    async def mlflow_stop_hook(input_data, tool_use_id, context):
        """Upload transcript as MLflow trace when agent stops.

        Awaits the upload so it completes before the event loop shuts down
        (the fresh-loop cleanup cancels pending tasks).  Matches the
        synchronous pattern used by the builder app.
        """
        session_id = input_data.get("session_id")
        transcript_path = input_data.get("transcript_path")

        print(f"    [MLflow] Stop hook fired: session={session_id}, transcript={transcript_path}")

        await _upload_trace_background(session_id, transcript_path)
        return {"continue": True}

    return mlflow_stop_hook


async def run_agent(
    prompt: str,
    skill_md: str | None = None,
    mcp_config: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    cwd: str | None = None,
    timeout_seconds: int = 0,
    model: str | None = None,
    mlflow_experiment: str | None = None,
    skill_name: str | None = None,
    tool_modules: list[str] | None = None,
    project_root: Path | None = None,
    settings_path: Path | None = None,
) -> AgentResult:
    """Run a Claude Code agent with optional skill injection.

    Args:
        prompt: The user prompt to send to the agent.
        skill_md: Optional SKILL.md content to inject as system prompt.
        mcp_config: Optional MCP server configuration dict.
            Keys are server names, values are McpServerConfig objects.
        allowed_tools: List of allowed tool names. Defaults to common builtins.
        cwd: Working directory for the agent. Defaults to current dir.
        timeout_seconds: Maximum execution time in seconds. 0 = no timeout.
        model: Override the model to use (via env var).
        tool_modules: Optional list of MCP tool modules to expose (e.g.
            ``["genie", "sql"]``).  When set, only tools from those modules
            are added to allowed_tools.  Sourced from manifest.yaml.
        project_root: Project root directory. If None, uses Path.cwd().
        settings_path: Explicit path to agent settings JSON file.

    Returns:
        AgentResult with response text, trace metrics, and raw events.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )
    except ImportError:
        return AgentResult(
            response_text="",
            trace_metrics=TraceMetrics(session_id="error"),
            success=False,
            error="claude-agent-sdk not installed. Install with: pip install claude-agent-sdk>=0.1.39",
        )

    # Enable MLflow autolog for automatic Claude Code session tracing.
    # This captures the full conversation (prompts, tool calls, results,
    # token usage) as an MLflow trace — retrievable via MlflowClient.
    # See: https://mlflow.org/docs/latest/genai/tracing/integrations/listing/claude_code
    try:
        import mlflow.anthropic
        mlflow.anthropic.autolog()
    except Exception as e:
        logger.debug("mlflow.anthropic.autolog() not available: %s", e)

    session_id = str(uuid.uuid4())
    events: list[AgentEvent] = []
    response_parts: list[str] = []

    # Auto-load MCP config from .mcp.json if not explicitly provided
    if mcp_config is None:
        mcp_config = _load_mcp_config(project_root=project_root)
        if mcp_config:
            logger.info("Auto-loaded MCP config: %s", list(mcp_config.keys()))

    # Build allowed_tools list.
    # bypassPermissions alone doesn't auto-approve MCP tool calls -- they must
    # also appear in allowed_tools.  Discover MCP tool names by importing the
    # server's tool registry so the agent can call them without a permission
    # prompt.
    _BUILTIN_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    if allowed_tools is None:
        if mcp_config:
            mcp_tool_names = _discover_mcp_tool_names(mcp_config, tool_modules=tool_modules)
            allowed_tools = _BUILTIN_TOOLS + mcp_tool_names
            logger.info("Allowed tools: %d builtin + %d MCP", len(_BUILTIN_TOOLS), len(mcp_tool_names))
        else:
            allowed_tools = list(_BUILTIN_TOOLS)

    env = _get_agent_env(project_root=project_root, settings_path=settings_path)
    if model:
        env["ANTHROPIC_MODEL"] = model
    # Ensure subprocess doesn't think it's nested inside another Claude Code session.
    # Instead of mutating os.environ (not thread-safe), exclude it from the subprocess env.
    env.pop("CLAUDECODE", None)

    # Pass environment to MCP server processes.
    # The MCP server config is serialized as JSON over stdio to the Claude CLI.
    # Passing the FULL os.environ can exceed the 1MB message buffer, so we only
    # include vars the MCP server actually needs: auth, PATH, HOME, and Python.
    if mcp_config:
        _ESSENTIAL_KEYS = {"PATH", "HOME", "USER", "SHELL", "LANG", "PYTHONPATH",
                           "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV"}
        mcp_env: dict[str, str] = {}
        # Essential vars for the subprocess to function
        for k in _ESSENTIAL_KEYS:
            if k in os.environ:
                mcp_env[k] = os.environ[k]
        # All DATABRICKS_* / MLFLOW_* auth vars from agent env
        for k, v in env.items():
            if k.startswith(("DATABRICKS_", "MLFLOW_")):
                mcp_env[k] = v
        # Also pick up any DATABRICKS_* from os.environ not already in agent env
        for k, v in os.environ.items():
            if k.startswith("DATABRICKS_") and k not in mcp_env:
                mcp_env[k] = v
        for _server_name, server_cfg in mcp_config.items():
            if "env" not in server_cfg:
                server_cfg["env"] = dict(mcp_env)
            else:
                # Merge auth vars into existing env without clobbering user overrides
                for k, v in mcp_env.items():
                    server_cfg["env"].setdefault(k, v)

    # Set up MLflow tracing via Stop hook (fire-and-forget for observability)
    mlflow_hook = _get_mlflow_stop_hook(
        mlflow_experiment=mlflow_experiment,
        skill_name=skill_name,
        project_root=project_root,
        settings_path=settings_path,
    )
    hooks = {}
    if mlflow_hook:
        hooks["Stop"] = [HookMatcher(hooks=[mlflow_hook])]

    # Capture stderr from the Claude subprocess for debugging
    stderr_lines: list[str] = []

    def _stderr_callback(line: str):
        stripped = line.strip()
        if stripped:
            stderr_lines.append(stripped)
            # Surface MCP-related errors at warning level so they're visible
            if any(kw in stripped.lower() for kw in ("error", "traceback", "import", "mcp", "failed")):
                logger.warning("[Claude stderr] %s", stripped)
            else:
                logger.debug("[Claude stderr] %s", stripped)

    # Log MCP configuration for debugging tool discovery issues
    if mcp_config:
        for srv_name, srv_cfg in mcp_config.items():
            cmd = srv_cfg.get("command", "?")
            args = srv_cfg.get("args", [])
            has_env = "env" in srv_cfg
            logger.info(
                "MCP server '%s': command=%s args=%s env_set=%s",
                srv_name, cmd, args, has_env,
            )
            print(f"    [MCP] Server '{srv_name}': {cmd} {' '.join(str(a) for a in args)}")
    else:
        logger.info("No MCP servers configured -- using builtin tools only: %s", allowed_tools)

    options = ClaudeAgentOptions(
        cwd=cwd or os.getcwd(),
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        mcp_servers=mcp_config or {},
        system_prompt=skill_md or "",
        setting_sources=[],  # No project skills -- we inject our own
        env=env,
        hooks=hooks if hooks else None,
        stderr=_stderr_callback,
        max_buffer_size=5 * 1024 * 1024,  # 5MB -- MCP tool schemas can exceed 1MB default
    )

    start_time = time.monotonic()

    # Use ClaudeSDKClient (not query()) -- Stop hooks only fire with the client.
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                now = datetime.now(timezone.utc)
                elapsed = time.monotonic() - start_time

                if timeout_seconds and elapsed > timeout_seconds:
                    events.append(
                        AgentEvent(
                            type="error",
                            timestamp=now,
                            data={"message": f"Timeout after {timeout_seconds}s"},
                        )
                    )
                    break

                # Dispatch on message type -- same pattern as builder app
                if isinstance(msg, AssistantMessage):
                    usage_data = {}
                    if hasattr(msg, "usage") and msg.usage:
                        usage_data = {
                            "input_tokens": getattr(msg.usage, "input_tokens", 0),
                            "output_tokens": getattr(msg.usage, "output_tokens", 0),
                            "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0),
                            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0),
                        }
                    events.append(
                        AgentEvent(
                            type="assistant_turn",
                            timestamp=now,
                            data={"usage": usage_data},
                        )
                    )

                    for block in getattr(msg, "content", []):
                        if isinstance(block, TextBlock):
                            response_parts.append(block.text)
                            events.append(
                                AgentEvent(
                                    type="text",
                                    timestamp=now,
                                    data={"text": block.text},
                                )
                            )
                        elif isinstance(block, ToolUseBlock):
                            events.append(
                                AgentEvent(
                                    type="tool_use",
                                    timestamp=now,
                                    data={
                                        "id": block.id,
                                        "name": block.name,
                                        "input": block.input if isinstance(block.input, dict) else {},
                                    },
                                )
                            )
                        elif isinstance(block, ToolResultBlock):
                            events.append(
                                AgentEvent(
                                    type="tool_result",
                                    timestamp=now,
                                    data={
                                        "tool_use_id": getattr(block, "tool_use_id", ""),
                                        "content": getattr(block, "content", ""),
                                        "is_error": getattr(block, "is_error", False),
                                    },
                                )
                            )

                elif isinstance(msg, UserMessage):
                    # Tool results come back as UserMessage with ToolResultBlock content
                    for block in getattr(msg, "content", []):
                        if isinstance(block, ToolResultBlock):
                            events.append(
                                AgentEvent(
                                    type="tool_result",
                                    timestamp=now,
                                    data={
                                        "tool_use_id": getattr(block, "tool_use_id", ""),
                                        "content": getattr(block, "content", ""),
                                        "is_error": getattr(block, "is_error", False),
                                    },
                                )
                            )

                elif isinstance(msg, ResultMessage):
                    events.append(
                        AgentEvent(
                            type="result",
                            timestamp=now,
                            data={
                                "session_id": getattr(msg, "session_id", session_id),
                                "duration_ms": getattr(msg, "duration_ms", None),
                                "cost": getattr(msg, "cost", None),
                            },
                        )
                    )
                    session_id = getattr(msg, "session_id", session_id)

                elif isinstance(msg, SystemMessage):
                    events.append(
                        AgentEvent(
                            type="system",
                            timestamp=now,
                            data={
                                "subtype": getattr(msg, "subtype", ""),
                                "data": getattr(msg, "data", {}),
                            },
                        )
                    )

    except asyncio.TimeoutError:
        events.append(
            AgentEvent(
                type="error",
                timestamp=datetime.now(timezone.utc),
                data={"message": f"asyncio.TimeoutError after {timeout_seconds}s"},
            )
        )
    except Exception as e:
        stderr_detail = "; ".join(stderr_lines[-5:]) if stderr_lines else "no stderr"
        logger.error("Agent execution failed: %s | stderr: %s", e, stderr_detail)
        events.append(
            AgentEvent(
                type="error",
                timestamp=datetime.now(timezone.utc),
                data={"message": f"{e} | stderr: {stderr_detail}"},
            )
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)

    # Build trace metrics from captured events
    trace_metrics = _build_trace_metrics(events, session_id)

    # Determine model from env
    trace_metrics.model = model or os.environ.get("ANTHROPIC_MODEL")

    response_text = "\n".join(response_parts)
    has_error = any(e.type == "error" for e in events)

    # Capture the MLflow trace ID if autolog created one.
    mlflow_trace_id = None
    try:
        import mlflow
        last_trace = mlflow.get_last_active_trace()
        if last_trace:
            mlflow_trace_id = last_trace.info.trace_id
            logger.info("MLflow trace captured: %s", mlflow_trace_id)
    except Exception:
        pass  # MLflow not available or no trace — fine

    return AgentResult(
        response_text=response_text,
        trace_metrics=trace_metrics,
        events=events,
        session_id=session_id,
        duration_ms=duration_ms,
        success=not has_error,
        error=next((e.data.get("message") for e in events if e.type == "error"), None),
        mlflow_trace_id=mlflow_trace_id,
    )


def _run_in_fresh_loop(coro) -> Any:
    """Run a coroutine in a fresh event loop in a dedicated thread.

    Same pattern as databricks-builder-app/server/services/agent.py --
    avoids subprocess transport cleanup errors by running the entire
    event loop lifecycle in an isolated thread.
    """
    import concurrent.futures

    result_holder: dict[str, Any] = {}

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_holder["value"] = loop.run_until_complete(coro)
        except Exception as e:
            result_holder["error"] = e
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                # Don't block on shutdown_default_executor() -- background tasks
                # (e.g. trace uploads) may still be running.
                try:
                    loop.run_until_complete(asyncio.wait_for(loop.shutdown_default_executor(), timeout=5.0))
                except (asyncio.TimeoutError, Exception):
                    pass  # Let the default executor GC naturally
            except Exception:
                pass
            # Suppress "Loop ... is closed" from subprocess transport __del__
            # that runs during GC after the loop closes. This is harmless --
            # the subprocess has already exited.
            _original_check = loop._check_closed
            loop._check_closed = lambda: None
            loop.close()

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_thread_target)
        future.result(timeout=3600)  # wait for thread to finish (1h max)
    except concurrent.futures.TimeoutError:
        # Don't let shutdown(wait=True) block -- the thread is still running
        pool.shutdown(wait=False)
        raise
    else:
        pool.shutdown(wait=True)

    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder["value"]


def run_agent_sync_wrapper(
    prompt: str,
    skill_md: str | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Synchronous wrapper for run_agent.

    Runs the async agent in a fresh event loop on a dedicated thread,
    following the same pattern as databricks-builder-app to avoid
    anyio cancel-scope and subprocess transport cleanup issues.
    """
    return _run_in_fresh_loop(run_agent(prompt=prompt, skill_md=skill_md, **kwargs))
