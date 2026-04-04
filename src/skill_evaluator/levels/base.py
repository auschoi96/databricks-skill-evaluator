"""Abstract base class for evaluation levels.

Each level implements a specific type of skill evaluation
(unit, integration, static, thinking, output).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..auth import WorkspaceConfig
    from ..skill_discovery import SkillDescriptor
    from ..mcp_resolver import MCPConfig
    from ..test_instructions import SkillTestInstructions


@dataclass
class LevelResult:
    """Result from running a single evaluation level."""

    level: str
    score: float
    feedbacks: list[dict[str, Any]] = field(default_factory=list)
    task_results: list[dict[str, Any]] | None = None
    artifacts: dict[str, str] | None = None
    metadata: dict[str, Any] | None = None
    trace_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.score >= 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "score": self.score,
            "passed": self.passed,
            "num_feedbacks": len(self.feedbacks),
            "feedbacks": self.feedbacks,
            "task_results": self.task_results,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
            "trace_ids": self.trace_ids,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LevelResult":
        return cls(
            level=data.get("level", ""),
            score=data.get("score", 0.0),
            feedbacks=data.get("feedbacks", []),
            task_results=data.get("task_results"),
            artifacts=data.get("artifacts"),
            metadata=data.get("metadata"),
            trace_ids=data.get("trace_ids", []),
        )


@dataclass
class LevelConfig:
    """Configuration passed to each evaluation level."""

    workspace: "WorkspaceConfig"
    skill: "SkillDescriptor"
    test_instructions: "SkillTestInstructions"
    mcp_config: Optional["MCPConfig"] = None
    agent_model: Optional[str] = None
    agent_timeout: int = 0  # 0 = no timeout
    judge_model: Optional[str] = None
    parallel_agents: int = 2
    prior_results: dict[str, "LevelResult"] = field(default_factory=dict)


class EvalLevel(ABC):
    """Abstract base class for evaluation levels."""

    @abstractmethod
    def run(self, config: LevelConfig) -> LevelResult:
        """Execute this evaluation level.

        Args:
            config: Full evaluation configuration.

        Returns:
            LevelResult with score, feedbacks, and artifacts.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this level (e.g., 'unit', 'static')."""
        ...

    @property
    @abstractmethod
    def level_number(self) -> int:
        """Numeric level (1-5) for ordering."""
        ...

    @property
    def requires_agent(self) -> bool:
        """Whether this level needs Claude Agent SDK execution."""
        return False

    @property
    def requires_workspace(self) -> bool:
        """Whether this level needs Databricks workspace access."""
        return False

    @property
    def requires_mcp(self) -> bool:
        """Whether this level needs MCP server connectivity."""
        return False
