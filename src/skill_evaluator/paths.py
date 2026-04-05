"""Standard directory paths for the skill evaluator.

Convention:
    skills/   — Copy skills to evaluate here (each as a subdirectory with SKILL.md)
    mcps/     — Clone MCP server repos here (e.g., databricks-mcp-server)
    .mcp.json — Centralized MCP config at repo root (references mcps/)
"""

from __future__ import annotations

from pathlib import Path

# Project root: walk up from this file to find pyproject.toml
_this = Path(__file__).resolve()
PROJECT_ROOT = _this.parent.parent.parent
for _candidate in [_this.parent.parent.parent, _this.parent.parent.parent.parent]:
    if (_candidate / "pyproject.toml").exists():
        PROJECT_ROOT = _candidate
        break

SKILLS_DIR = PROJECT_ROOT / "skills"
MCPS_DIR = PROJECT_ROOT / "mcps"
DEFAULT_MCP_JSON = PROJECT_ROOT / ".mcp.json"


def resolve_skill_dir(skill_ref: str) -> Path:
    """Resolve a skill reference to an absolute directory path.

    Accepts:
        - A bare name like "databricks-genie" → skills/databricks-genie
        - A relative or absolute path like "./my-skill" or /tmp/my-skill
    """
    path = Path(skill_ref)

    # If it's an existing path (relative or absolute), use it directly
    if path.exists() and (path / "SKILL.md").exists():
        return path.resolve()

    # Try as a skill name in the standard directory
    candidate = SKILLS_DIR / skill_ref
    if candidate.exists() and (candidate / "SKILL.md").exists():
        return candidate.resolve()

    # Fall back to the original path (let downstream code handle the error)
    return path.resolve()


def list_available_skills() -> list[str]:
    """List skill names in the standard skills/ directory."""
    if not SKILLS_DIR.exists():
        return []
    return sorted(
        d.name for d in SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    )
