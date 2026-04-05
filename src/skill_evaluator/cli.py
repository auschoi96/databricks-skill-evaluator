"""Click-based CLI for databricks-skill-evaluator.

Usage:
    dse auth --profile e2-demo-field-eng --catalog ac_demo --schema skill_test
    dse init /path/to/skill
    dse evaluate /path/to/skill --levels all --mcp-json .mcp.json
    dse compare /path/to/skill --baseline-run-id abc123
    dse optimize /path/to/skill --preset quick --apply
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

logger = logging.getLogger("skill_evaluator")


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def main(verbose: bool):
    """Databricks Skill Evaluator — evaluate and optimize Claude Code skills."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option("--profile", default="DEFAULT", help="Databricks config profile")
@click.option("--catalog", default=None, help="Unity Catalog to use for test resources")
@click.option("--schema", default=None, help="Schema to use for test resources")
@click.option("--warehouse-id", default=None, help="SQL warehouse ID (auto-detected if omitted)")
@click.option("--experiment", default=None, help="MLflow experiment path")
def auth(profile: str, catalog: str, schema: str, warehouse_id: str, experiment: str):
    """Authenticate with Databricks and save config."""
    from .auth import authenticate, AuthError

    try:
        config = authenticate(
            profile=profile,
            catalog=catalog,
            schema=schema,
            warehouse_id=warehouse_id,
            experiment_path=experiment,
        )
        click.echo(f"Authenticated as profile '{config.profile}' on {config.host}")
        click.echo(f"  Catalog: {config.catalog}")
        click.echo(f"  Schema: {config.schema}")
        click.echo(f"  Warehouse: {config.warehouse_id or '(auto-detect)'}")
        click.echo(f"  Experiment: {config.experiment_path}")
        click.echo(f"Config saved to ~/.dse/config.yaml")
    except AuthError as e:
        click.echo(f"Authentication failed:\n{e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--profile", default="DEFAULT", help="Databricks config profile from ~/.databrickscfg")
def setup(profile: str):
    """Register the skill-evaluator MCP server in ~/.claude.json.

    Automatically detects Python path, run_server.py location, and
    Databricks credentials from the given profile. After running this,
    restart Claude Code and verify with /mcp.
    """
    import json
    import subprocess
    import configparser

    claude_json_path = Path.home() / ".claude.json"

    # 1. Find Python and run_server.py
    python_path = sys.executable
    run_server_path = str(Path(__file__).resolve().parent.parent.parent / "run_server.py")
    if not Path(run_server_path).exists():
        # Fallback: look relative to the installed package
        import skill_evaluator
        pkg_root = Path(skill_evaluator.__file__).resolve().parent.parent.parent
        run_server_path = str(pkg_root / "run_server.py")
    if not Path(run_server_path).exists():
        click.echo("Error: Could not find run_server.py. Provide the path to the repo root.", err=True)
        sys.exit(1)

    # 2. Get Databricks host from profile
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        click.echo("Error: No ~/.databrickscfg found. Run: databricks auth login --host <URL>", err=True)
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    if profile not in cfg:
        available = [s for s in cfg.sections() if s != "DEFAULT"]
        click.echo(f"Error: Profile '{profile}' not found. Available: {', '.join(available)}", err=True)
        sys.exit(1)

    host = cfg[profile].get("host", "").rstrip("/")
    if not host:
        click.echo(f"Error: No host in profile '{profile}'", err=True)
        sys.exit(1)

    # 3. Get a fresh token
    try:
        result = subprocess.run(
            ["databricks", "auth", "token", "--profile", profile],
            capture_output=True, text=True, timeout=30,
        )
        token_data = json.loads(result.stdout)
        token = token_data["access_token"]
    except Exception as e:
        click.echo(f"Error getting token: {e}", err=True)
        click.echo("You may need to run: databricks auth login --host <URL>", err=True)
        sys.exit(1)

    # 4. Read or create ~/.claude.json
    if claude_json_path.exists():
        with open(claude_json_path) as f:
            claude_data = json.load(f)
    else:
        claude_data = {}

    if "mcpServers" not in claude_data:
        claude_data["mcpServers"] = {}

    # 5. Add/update skill-evaluator entry
    claude_data["mcpServers"]["skill-evaluator"] = {
        "command": python_path,
        "args": [run_server_path],
        "env": {
            "DATABRICKS_CONFIG_PROFILE": profile,
            "DATABRICKS_HOST": host,
            "DATABRICKS_TOKEN": token,
        },
    }

    with open(claude_json_path, "w") as f:
        json.dump(claude_data, f, indent=2)

    # 6. Auto-approve skill-evaluator MCP tools in Claude Code settings
    settings_dir = Path.home() / ".claude"
    settings_dir.mkdir(exist_ok=True)
    settings_path = settings_dir / "settings.json"

    if settings_path.exists():
        with open(settings_path) as f:
            settings_data = json.load(f)
    else:
        settings_data = {}

    if "permissions" not in settings_data:
        settings_data["permissions"] = {}
    if "allow" not in settings_data["permissions"]:
        settings_data["permissions"]["allow"] = []

    tool_pattern = "mcp__skill-evaluator__*"
    if tool_pattern not in settings_data["permissions"]["allow"]:
        settings_data["permissions"]["allow"].append(tool_pattern)

    with open(settings_path, "w") as f:
        json.dump(settings_data, f, indent=2)

    click.echo(f"Registered skill-evaluator MCP server in {claude_json_path}")
    click.echo(f"Auto-approved skill-evaluator tools in {settings_path}")
    click.echo(f"  Python:  {python_path}")
    click.echo(f"  Server:  {run_server_path}")
    click.echo(f"  Profile: {profile}")
    click.echo(f"  Host:    {host}")
    click.echo(f"  Token:   {token[:20]}... (expires ~1hr for OAuth)")
    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Restart Claude Code")
    click.echo("  2. Run /mcp to verify skill-evaluator shows as connected")
    click.echo("  3. Say: evaluate my skill at /path/to/my-skill")
    click.echo("  All skill-evaluator tools will run without manual approval.")
    click.echo()
    click.echo("To refresh an expired token, re-run: dse setup --profile " + profile)


@main.command(name="list")
def list_skills():
    """List available skills in the skills/ directory."""
    from .paths import SKILLS_DIR, list_available_skills

    skills = list_available_skills()
    if not skills:
        click.echo(f"No skills found in {SKILLS_DIR}/")
        click.echo(f"Copy a skill directory into {SKILLS_DIR}/ to get started.")
        return

    click.echo(f"Available skills ({SKILLS_DIR}/):\n")
    for name in skills:
        skill_path = SKILLS_DIR / name
        has_eval = (skill_path / "eval" / "ground_truth.yaml").exists()
        status = "ready" if has_eval else "needs init"
        click.echo(f"  {name:30s} [{status}]")


@main.command()
@click.argument("skill_dir", type=click.Path())
def init(skill_dir: str):
    """Initialize eval/ config for a skill directory.

    SKILL_DIR can be a name (resolved from skills/) or a path.
    """
    from .paths import resolve_skill_dir
    from .skill_discovery import SkillDescriptor, SkillDiscoveryError
    from .test_instructions import init_eval_config

    resolved = resolve_skill_dir(skill_dir)
    try:
        skill = SkillDescriptor.from_directory(resolved)
        eval_dir = init_eval_config(resolved, skill.name)
        click.echo(f"Initialized eval config for '{skill.name}' in {eval_dir}")
        click.echo(f"Files created:")
        for f in sorted(eval_dir.rglob("*")):
            if f.is_file():
                click.echo(f"  {f.relative_to(eval_dir)}")
    except SkillDiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument("skill_dir", type=click.Path())
@click.option("--levels", default="unit,static", help="Comma-separated levels: unit,integration,static,thinking,output,all")
@click.option("--mcp-json", type=click.Path(), default=None, help="Path to .mcp.json for MCP tools (defaults to repo root .mcp.json)")
@click.option("--profile", default=None, help="Databricks config profile (uses saved config if omitted)")
@click.option("--catalog", default=None, help="Unity Catalog override")
@click.option("--schema", default=None, help="Schema override")
@click.option("--experiment", default=None, help="MLflow experiment path override")
@click.option("--agent-model", default=None, help="Claude model for agent execution")
@click.option("--agent-timeout", default=0, type=int, help="Agent timeout in seconds (0 = no timeout)")
@click.option("--judge-model", default=None, help="LLM model for judge evaluations")
@click.option("--suggest-improvements", is_flag=True, help="Generate improvement suggestions")
@click.option("--compare-baseline", default=None, help="MLflow run ID to compare against")
def evaluate(
    skill_dir: str,
    levels: str,
    mcp_json: str,
    profile: str,
    catalog: str,
    schema: str,
    experiment: str,
    agent_model: str,
    agent_timeout: int,
    judge_model: str,
    suggest_improvements: bool,
    compare_baseline: str,
):
    """Run evaluation on a skill directory.

    SKILL_DIR can be a name (resolved from skills/) or a path.
    """
    from .auth import WorkspaceConfig, load_config, authenticate, AuthError
    from .mcp_resolver import MCPConfig
    from .orchestrator import EvaluationSuiteConfig, run_evaluation_suite
    from .paths import resolve_skill_dir, DEFAULT_MCP_JSON
    from .skill_discovery import SkillDescriptor, SkillDiscoveryError
    from .test_instructions import SkillTestInstructions

    # Resolve skill directory (name or path)
    resolved_dir = resolve_skill_dir(skill_dir)

    # Parse skill
    try:
        skill = SkillDescriptor.from_directory(resolved_dir)
    except SkillDiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Skill: {skill.name}")
    click.echo(f"  Description: {skill.description[:80]}...")
    click.echo(f"  Reference files: {len(skill.reference_files)}")
    click.echo(f"  MCP tool references: {len(skill.mcp_tool_references)}")

    # Load or create workspace config
    ws_config = load_config(profile)
    if not ws_config:
        try:
            ws_config = authenticate(
                profile=profile or "DEFAULT",
                catalog=catalog,
                schema=schema,
                experiment_path=experiment,
            )
        except AuthError as e:
            click.echo(f"Authentication failed:\n{e}", err=True)
            sys.exit(1)

    # Apply overrides
    if catalog:
        ws_config.catalog = catalog
    if schema:
        ws_config.schema = schema
    if experiment:
        ws_config.experiment_path = experiment

    # Load MCP config (explicit > repo root default > auto-discover)
    mcp_config = None
    if mcp_json:
        mcp_config = MCPConfig.from_mcp_json(Path(mcp_json))
    elif DEFAULT_MCP_JSON.exists():
        mcp_config = MCPConfig.from_mcp_json(DEFAULT_MCP_JSON)
    else:
        mcp_config = MCPConfig.auto_discover(resolved_dir)

    if mcp_config:
        mcp_config.resolve_available_tools()

    # Load test instructions
    test_instructions = SkillTestInstructions.from_skill_dir(resolved_dir)

    # Parse levels
    level_list = [l.strip() for l in levels.split(",")]

    click.echo(f"\nRunning levels: {', '.join(level_list)}")
    click.echo(f"Experiment: {ws_config.experiment_path}")
    click.echo()

    # Run evaluation
    suite_config = EvaluationSuiteConfig(
        workspace=ws_config,
        skill=skill,
        test_instructions=test_instructions,
        mcp_config=mcp_config,
        levels=level_list,
        agent_model=agent_model,
        agent_timeout=agent_timeout,
        judge_model=judge_model,
        suggest_improvements=suggest_improvements,
        compare_baseline_run_id=compare_baseline,
    )

    result = run_evaluation_suite(suite_config)

    # Print summary
    click.echo(f"\n{'='*60}")
    click.echo(f"RESULTS: {skill.name}")
    click.echo(f"{'='*60}")
    for level_name, level_result in result.level_results.items():
        status = "PASS" if level_result.passed else "FAIL"
        color = "green" if level_result.passed else "red"
        click.echo(
            click.style(f"  L{_level_num(level_name)}: {level_name:15s} {level_result.score:.0%} [{status}]", fg=color)
        )
    click.echo(f"  {'─'*40}")
    composite_color = "green" if result.composite_score >= 0.8 else "yellow" if result.composite_score >= 0.5 else "red"
    click.echo(click.style(f"  Composite:        {result.composite_score:.0%}", fg=composite_color, bold=True))

    if result.suggestions:
        click.echo(f"\nSuggestions:")
        for s in result.suggestions[:10]:
            click.echo(f"  - {s}")

    if result.mlflow_run_id:
        click.echo(f"\nMLflow run: {result.mlflow_run_id}")

    # Check if report was generated
    report_path = resolved_dir / "eval" / "report.html"
    if report_path.exists():
        click.echo(f"HTML report: {report_path}")


@main.command()
@click.argument("skill_dir", type=click.Path(exists=True))
@click.option("--preset", default="quick", type=click.Choice(["minimal", "quick", "standard", "thorough"]))
@click.option("--feedback", type=click.Path(exists=True), default=None, help="Path to feedback.json")
@click.option("--apply", "apply_result", is_flag=True, help="Apply optimized SKILL.md immediately")
def optimize(skill_dir: str, preset: str, feedback: str, apply_result: bool):
    """Run GEPA optimization on a skill."""
    click.echo(f"Optimization with preset '{preset}' — coming in next iteration")
    click.echo("This will use the GEPA optimization loop from the optimize/ module")


def _level_num(level_name: str) -> int:
    order = {"unit": 1, "integration": 2, "static": 3, "thinking": 4, "output": 5}
    return order.get(level_name, 0)


if __name__ == "__main__":
    main()
