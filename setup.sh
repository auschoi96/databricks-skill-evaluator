#!/bin/bash
#
# Setup script for databricks-skill-evaluator
#
# Installs the evaluator and (optionally) the Databricks MCP server
# so that agent-based evaluation levels (L2/L4/L5) can call MCP tools.
#
# Usage:
#   ./setup.sh              # Install evaluator only
#   ./setup.sh --with-mcp   # Install evaluator + Databricks MCP server
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${SCRIPT_DIR}/skills"
MCPS_DIR="${SCRIPT_DIR}/mcps"
MCP_SERVER_DIR="${MCPS_DIR}/databricks-mcp-server"
TOOLS_CORE_DIR="${MCPS_DIR}/databricks-tools-core"

echo "======================================"
echo "Databricks Skill Evaluator Setup"
echo "======================================"
echo ""

# ── 1. Install the evaluator ─────────────────────────────────────────
echo "Installing databricks-skill-evaluator..."
pip install -e "$SCRIPT_DIR" --quiet
echo "  dse CLI installed"

# Verify
if ! command -v dse &> /dev/null; then
    echo "Warning: 'dse' not on PATH. You may need to activate your venv."
fi

# ── 2. (Optional) Install MCP server for example evals ───────────────
if [[ "$1" == "--with-mcp" ]]; then
    echo ""
    echo "Setting up Databricks MCP server..."

    if [ ! -d "$MCP_SERVER_DIR" ]; then
        echo "Error: databricks-mcp-server not found at $MCP_SERVER_DIR"
        echo "Clone it into mcps/ first:"
        echo "  git clone <repo-url> $MCP_SERVER_DIR"
        exit 1
    fi

    if [ ! -d "$TOOLS_CORE_DIR" ]; then
        echo "Error: databricks-tools-core not found at $TOOLS_CORE_DIR"
        echo "Clone it into mcps/ first:"
        echo "  git clone <repo-url> $TOOLS_CORE_DIR"
        exit 1
    fi

    echo "  Installing databricks-tools-core..."
    pip install -e "$TOOLS_CORE_DIR" --quiet

    echo "  Installing databricks-mcp-server..."
    pip install -e "$MCP_SERVER_DIR" --quiet

    # Verify import
    if python -c "import databricks_mcp_server" 2>/dev/null; then
        echo "  Databricks MCP server ready"
    else
        echo "Error: Failed to import databricks_mcp_server"
        exit 1
    fi
fi

# ── 3. Summary ────────────────────────────────────────────────────────
echo ""
echo "======================================"
echo "Setup complete!"
echo "======================================"
echo ""
echo "Quick start:"
echo "  dse auth --profile <your-databricks-profile>"
echo "  dse evaluate <skill_dir> --levels unit,static"
echo ""

if [[ "$1" != "--with-mcp" ]]; then
    echo "To also install the Databricks MCP server (needed for agent evals):"
    echo "  ./setup.sh --with-mcp"
    echo ""
fi
