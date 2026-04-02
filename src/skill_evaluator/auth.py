"""Guided Databricks authentication flow.

Validates workspace connectivity, discovers SQL warehouses, and
saves configuration for reuse across evaluation sessions.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_DSE_CONFIG_DIR = Path.home() / ".dse"
_DSE_CONFIG_PATH = _DSE_CONFIG_DIR / "config.yaml"


@dataclass
class WorkspaceConfig:
    """Validated Databricks workspace configuration."""

    profile: str
    host: str
    catalog: str
    schema: str
    warehouse_id: Optional[str] = None
    experiment_path: str = "/Shared/skill-evals"

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "host": self.host,
            "catalog": self.catalog,
            "schema": self.schema,
            "warehouse_id": self.warehouse_id,
            "experiment_path": self.experiment_path,
        }


def authenticate(
    profile: str = "DEFAULT",
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    warehouse_id: Optional[str] = None,
    experiment_path: Optional[str] = None,
    interactive: bool = True,
) -> WorkspaceConfig:
    """Guide user through Databricks authentication.

    1. Check ~/.databrickscfg for the specified profile
    2. Validate connectivity via WorkspaceClient
    3. Discover available SQL warehouses
    4. Prompt for catalog/schema if not provided
    5. Save config to ~/.dse/config.yaml
    """
    # Step 1: Check profile exists
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        raise AuthError(
            "No ~/.databrickscfg found. Run:\n"
            f"  databricks auth login --host <YOUR_WORKSPACE_URL> --profile {profile}"
        )

    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    if profile not in cfg:
        available = [s for s in cfg.sections() if s != "DEFAULT"]
        raise AuthError(
            f"Profile '{profile}' not found in ~/.databrickscfg.\n"
            f"Available profiles: {', '.join(available) or '(none)'}\n"
            f"Run: databricks auth login --host <URL> --profile {profile}"
        )

    # Step 2: Validate connectivity
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient(profile=profile)
        me = w.current_user.me()
        host = w.config.host
        logger.info(f"Authenticated as {me.user_name} on {host}")
    except Exception as e:
        raise AuthError(
            f"Cannot connect to workspace with profile '{profile}': {e}\n"
            f"Run: databricks auth login --host <URL> --profile {profile}"
        ) from e

    # Step 3: Discover SQL warehouses
    discovered_warehouse = warehouse_id
    if not discovered_warehouse:
        try:
            warehouses = list(w.warehouses.list())
            running = [wh for wh in warehouses if str(wh.state) == "RUNNING"]
            if running:
                discovered_warehouse = running[0].id
                logger.info(f"Auto-detected warehouse: {running[0].name} ({discovered_warehouse})")
            elif warehouses:
                discovered_warehouse = warehouses[0].id
                logger.info(f"Using first available warehouse: {warehouses[0].name}")
        except Exception as e:
            logger.warning(f"Could not list warehouses: {e}")

    # Step 4: Use provided or default catalog/schema
    final_catalog = catalog or "main"
    final_schema = schema or "skill_test"

    # Step 5: Validate catalog/schema exist
    _validate_catalog_schema(w, final_catalog, final_schema)

    config = WorkspaceConfig(
        profile=profile,
        host=host,
        catalog=final_catalog,
        schema=final_schema,
        warehouse_id=discovered_warehouse,
        experiment_path=experiment_path or "/Shared/skill-evals",
    )

    # Save config
    save_config(config)

    # Apply environment variables for MLflow
    os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    os.environ["DATABRICKS_HOST"] = host

    return config


def validate_workspace(config: WorkspaceConfig) -> list[str]:
    """Verify workspace is accessible and resources exist.

    Returns list of validation errors (empty = success).
    """
    errors = []

    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient(profile=config.profile)
        w.current_user.me()
    except Exception as e:
        errors.append(f"Cannot connect to workspace: {e}")
        return errors

    try:
        _validate_catalog_schema(w, config.catalog, config.schema)
    except AuthError as e:
        errors.append(str(e))

    if config.warehouse_id:
        try:
            wh = w.warehouses.get(config.warehouse_id)
            if str(wh.state) != "RUNNING":
                errors.append(f"Warehouse {config.warehouse_id} is {wh.state}, not RUNNING")
        except Exception as e:
            errors.append(f"Cannot access warehouse {config.warehouse_id}: {e}")

    return errors


def load_config(profile: str | None = None) -> WorkspaceConfig | None:
    """Load saved config from ~/.dse/config.yaml."""
    if not _DSE_CONFIG_PATH.exists():
        return None

    with open(_DSE_CONFIG_PATH) as f:
        data = yaml.safe_load(f) or {}

    profiles = data.get("profiles", {})
    target = profile or data.get("default_profile", "DEFAULT")

    if target not in profiles:
        return None

    p = profiles[target]
    return WorkspaceConfig(
        profile=target,
        host=p.get("host", ""),
        catalog=p.get("catalog", "main"),
        schema=p.get("schema", "skill_test"),
        warehouse_id=p.get("warehouse_id"),
        experiment_path=p.get("experiment_path", "/Shared/skill-evals"),
    )


def save_config(config: WorkspaceConfig) -> None:
    """Save config to ~/.dse/config.yaml."""
    _DSE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    data = {}
    if _DSE_CONFIG_PATH.exists():
        with open(_DSE_CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}

    data.setdefault("profiles", {})
    data["default_profile"] = config.profile
    data["profiles"][config.profile] = config.to_dict()

    with open(_DSE_CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Config saved to {_DSE_CONFIG_PATH}")


def _validate_catalog_schema(w, catalog: str, schema: str) -> None:
    """Validate catalog and schema exist in Unity Catalog."""
    try:
        w.catalogs.get(catalog)
    except Exception:
        raise AuthError(
            f"Catalog '{catalog}' not found or not accessible.\n"
            f"Check your permissions or specify a different catalog with --catalog."
        )

    try:
        w.schemas.get(f"{catalog}.{schema}")
    except Exception:
        logger.warning(
            f"Schema '{catalog}.{schema}' not found. "
            f"It will be created during integration tests if needed."
        )


class AuthError(Exception):
    """Raised when authentication or workspace validation fails."""
    pass
