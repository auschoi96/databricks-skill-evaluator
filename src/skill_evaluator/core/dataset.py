"""DatasetSource abstraction — YAML-only initially, UC interface defined for later.

Extracted from ai-dev-kit/.test/src/skill_test/dataset.py.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional, Protocol
import yaml


@dataclass
class EvalRecord:
    """Standard evaluation record format (matches databricks-mlflow-evaluation patterns)."""

    id: str
    inputs: Dict[str, Any]
    outputs: Optional[Dict[str, Any]] = None
    expectations: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_eval_dict(self) -> Dict[str, Any]:
        """Convert to MLflow evaluation format."""
        result = {"inputs": self.inputs}
        if self.outputs:
            result["outputs"] = self.outputs
        if self.expectations:
            result["expectations"] = self.expectations
        return result


class DatasetSource(Protocol):
    """Protocol for dataset sources — enables future UC integration."""

    def load(self) -> List[EvalRecord]:
        """Load evaluation records."""
        ...


@dataclass
class YAMLDatasetSource:
    """Load evaluation dataset from YAML file."""

    yaml_path: Path

    def load(self) -> List[EvalRecord]:
        """Load records from YAML ground_truth.yaml file."""
        with open(self.yaml_path) as f:
            data = yaml.safe_load(f)

        yaml_dir = self.yaml_path.parent

        records = []
        for case in data.get("test_cases", []):
            outputs = case.get("outputs")

            if outputs and "expected_response_file" in outputs:
                response_file = yaml_dir / outputs["expected_response_file"]
                if response_file.exists():
                    with open(response_file) as rf:
                        outputs = dict(outputs)
                        outputs["response"] = rf.read()
                        del outputs["expected_response_file"]

            records.append(
                EvalRecord(
                    id=case["id"],
                    inputs=case["inputs"],
                    outputs=outputs,
                    expectations=case.get("expectations"),
                    metadata=case.get("metadata", {}),
                )
            )
        return records

    def save(self, records: List[EvalRecord]) -> None:
        """Save records back to YAML file."""
        data = {
            "test_cases": [
                {
                    "id": r.id,
                    "inputs": r.inputs,
                    "outputs": r.outputs,
                    "expectations": r.expectations,
                    "metadata": r.metadata,
                }
                for r in records
            ]
        }
        with open(self.yaml_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def get_dataset_source(skill_name: str, base_path: Path | None = None) -> DatasetSource:
    """Get the appropriate dataset source for a skill.

    Args:
        skill_name: Name of the skill to load data for.
        base_path: Directory containing skill eval directories. If None,
                   searches common paths relative to CWD.
    """
    if base_path is None:
        for candidate in [Path("eval"), Path("skills"), Path(".test/skills")]:
            if candidate.exists():
                base_path = candidate
                break
        else:
            base_path = Path(".")

    yaml_path = base_path / skill_name / "ground_truth.yaml"
    if not yaml_path.exists():
        # Also check eval/ subdirectory pattern
        yaml_path = base_path / skill_name / "eval" / "ground_truth.yaml"

    if yaml_path.exists():
        return YAMLDatasetSource(yaml_path)

    raise FileNotFoundError(f"No ground_truth.yaml found for {skill_name} in {base_path}")
