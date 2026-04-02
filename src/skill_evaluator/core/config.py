"""Configuration for databricks-skill-evaluator framework.

Generalized from ai-dev-kit/.test/src/skill_test/config.py.
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class QualityGate:
    """A single quality gate threshold."""

    metric: str
    threshold: float
    comparison: str = ">="  # >=, >, ==, <, <=


@dataclass
class QualityGates:
    """Quality thresholds that must pass for evaluation success."""

    gates: List[QualityGate] = field(
        default_factory=lambda: [
            QualityGate("syntax_valid/score/mean", 1.0),
            QualityGate("pattern_adherence/score/mean", 0.90),
            QualityGate("no_hallucinated_apis/score/mean", 1.0),
            QualityGate("execution_success/score/mean", 0.80),
        ]
    )


@dataclass
class DatabricksAuthConfig:
    """Databricks authentication configuration.

    Uses OAuth via config profile by default. The profile should be configured
    in ~/.databrickscfg with OAuth credentials.
    """

    config_profile: str = field(default_factory=lambda: os.getenv("DATABRICKS_CONFIG_PROFILE", "DEFAULT"))

    def apply(self) -> None:
        """Apply auth config by setting environment variables for MLflow."""
        os.environ["DATABRICKS_CONFIG_PROFILE"] = self.config_profile

        if os.getenv("DATABRICKS_HOST"):
            return

        try:
            from databricks.sdk import WorkspaceClient

            w = WorkspaceClient(profile=self.config_profile)
            os.environ["DATABRICKS_HOST"] = w.config.host
        except Exception:
            import configparser
            from pathlib import Path

            cfg_path = Path.home() / ".databrickscfg"
            if cfg_path.exists():
                config = configparser.ConfigParser()
                config.read(cfg_path)
                if self.config_profile in config:
                    host = config[self.config_profile].get("host")
                    if host:
                        os.environ["DATABRICKS_HOST"] = host


@dataclass
class MLflowConfig:
    """MLflow configuration from environment variables."""

    tracking_uri: str = field(default_factory=lambda: _get_mlflow_tracking_uri())
    experiment_name: str = field(default_factory=lambda: os.getenv("MLFLOW_EXPERIMENT_NAME", "/Shared/skill-evals"))
    llm_judge_timeout: int = field(
        default_factory=lambda: int(os.getenv("MLFLOW_LLM_JUDGE_TIMEOUT", "120"))
    )


def _get_mlflow_tracking_uri() -> str:
    """Determine MLflow tracking URI, respecting DATABRICKS_CONFIG_PROFILE."""
    if os.getenv("MLFLOW_TRACKING_URI"):
        return os.getenv("MLFLOW_TRACKING_URI")

    profile = os.getenv("DATABRICKS_CONFIG_PROFILE")
    if profile:
        return f"databricks://{profile}"

    return "databricks"


@dataclass
class DatabricksExecutionSettings:
    """Settings for Databricks code execution."""

    cluster_id: Optional[str] = None
    warehouse_id: Optional[str] = None
    use_serverless: bool = True

    catalog: str = field(default_factory=lambda: os.getenv("SKILL_TEST_CATALOG", "main"))
    schema: str = field(default_factory=lambda: os.getenv("SKILL_TEST_SCHEMA", "skill_test"))

    timeout: int = 240
    preserve_context: bool = True


@dataclass
class EvaluatorConfig:
    """Main configuration for databricks-skill-evaluator."""

    auth: DatabricksAuthConfig = field(default_factory=DatabricksAuthConfig)
    quality_gates: QualityGates = field(default_factory=QualityGates)
    mlflow: MLflowConfig = field(default_factory=MLflowConfig)
    databricks: DatabricksExecutionSettings = field(default_factory=DatabricksExecutionSettings)

    # Paths — configurable, no hardcoded defaults
    skills_root: Optional[str] = None
    eval_definitions_path: Optional[str] = None

    def __post_init__(self):
        """Apply auth configuration on initialization."""
        self.auth.apply()
