#!/usr/bin/env bash
# fl-native-bridge - installer (Windows x64 only)
#
# The bridge ships a compiled extension built against the Python 3.12 runtime
# bundled with FL Studio, so it only runs on Windows x64. No virtual MIDI port is
# needed: the MCP server and the controller talk over a file-queue RPC.
#
# What this does:
#   1. Checks the platform and finds FL Studio's Hardware folder
#   2. Copies the controller files (including _flmcp_native.pyd)
#   3. Installs the Python dependencies with uv
#   4. Registers the MCP server with Claude Desktop / Claude Code

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

echo -e "${BLUE}╔════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  fl-native-bridge - Installation           ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════╝${NC}"
echo ""

# Step 1: platform check
echo -e "${YELLOW}[1/4]${NC} Checking platform..."
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *)
        echo -e "${RED}This bridge ships a Windows x64 extension built for the Python 3.12${NC}"
        echo -e "${RED}runtime bundled with FL Studio. It does not run on $(uname -s).${NC}"
        exit 1
        ;;
esac
echo -e "${GREEN}✓${NC} Windows detected"

# Step 2: locate FL Studio's Hardware folder
echo ""
echo -e "${YELLOW}[2/4]${NC} Locating FL Studio..."

if [ -n "${FL_HARDWARE_DIR:-}" ]; then
    FL_DIR="$FL_HARDWARE_DIR"
else
    USER_HOME="$(cygpath -u "${USERPROFILE:-$HOME}" 2>/dev/null || echo "$HOME")"
    FL_DIR="$USER_HOME/Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP"
fi

if [ ! -d "$(dirname "$FL_DIR")" ]; then
    echo -e "${RED}Could not find FL Studio's Hardware folder at:${NC}"
    echo "  $(dirname "$FL_DIR")"
    echo ""
    echo "Start FL Studio once so it creates its Settings folder, or set FL_HARDWARE_DIR."
    exit 1
fi

mkdir -p "$FL_DIR"
echo -e "${GREEN}✓${NC} Target: $FL_DIR"

# Step 3: copy the controller files
echo ""
echo -e "${YELLOW}[3/4]${NC} Installing controller files..."
for f in device_FLStudioMCP.py semantic_native.py bridge_extension.py native_rpc.py _flmcp_native.pyd; do
    if [ ! -f "$SCRIPT_DIR/fl_controller/$f" ]; then
        echo -e "${RED}Missing fl_controller/$f${NC}"
        exit 1
    fi
    cp "$SCRIPT_DIR/fl_controller/$f" "$FL_DIR/$f"
done
echo -e "${GREEN}✓${NC} 5 files copied (controller + native extension)"

# Step 4: Python dependencies
echo ""
echo -e "${YELLOW}[4/4]${NC} Installing Python dependencies..."
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
uv sync
echo -e "${GREEN}✓${NC} Dependencies installed"

# Optional: register with Claude
echo ""
read -r -p "Register the MCP server with Claude Desktop/Code now? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
    bash "$SCRIPT_DIR/scripts/install_mcp_for_claude.sh"
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Installation complete                     ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Next steps:"
echo -e "  1. ${BLUE}Restart FL Studio${NC}"
echo -e "  2. ${BLUE}Options → MIDI settings${NC}: set the controller type to ${BLUE}FLStudioMCP${NC}"
echo -e "     and enable its port (no virtual MIDI device is required)."
echo -e "  3. ${BLUE}Restart your MCP client${NC} so it picks up the server."
echo ""
echo -e "Verify the bridge is live:"
echo -e "  ${YELLOW}uv run fl-native-bridge${NC}   then call ${YELLOW}fl_bridge_status${NC}"
echo ""
echo -e "${YELLOW}Note:${NC} the extension is version-locked. If FL reports a different"
echo -e "engine profile, the bridge refuses to load instead of writing blindly."
