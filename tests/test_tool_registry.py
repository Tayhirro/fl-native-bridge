"""Guard the MCP tool registry against duplicate/superseded tools.

Two near-identical tools confuse MCP clients into calling the wrong one:
- fl_clone_mixer_effect (partial, named parameters only) vs
  fl_clone_mixer_effect_full (complete event-213 state)
- fl_load_effect_preset (GUI drag-and-drop) vs
  fl_load_plugin_preset (native exact-slot load)

The older tools were removed. These tests fail if either is reintroduced.
"""

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fl_studio_mcp.server as srv  # noqa: E402


def _names():
    return {t.name for t in asyncio.run(srv.mcp.list_tools())}


def test_superseded_tools_are_not_registered():
    names = _names()
    assert "fl_clone_mixer_effect" not in names
    assert "fl_load_effect_preset" not in names


def test_replacement_tools_are_registered():
    names = _names()
    assert "fl_clone_mixer_effect_full" in names
    assert "fl_load_plugin_preset" in names


def test_no_near_duplicate_tool_names():
    """No two registered tools may differ only by a superseding suffix."""
    names = _names()
    suffixes = ("full", "at_slot", "preset", "info", "state")
    stems = {}
    for name in names:
        stem = re.sub(r"_(?:%s)$" % "|".join(suffixes), "", name)
        stems.setdefault(stem, []).append(name)
    overlaps = {stem: group for stem, group in stems.items() if len(group) > 1}
    # fl_get_piano_roll_info/state are deliberately distinct: integration
    # status vs note state. Everything else must be unique.
    unexpected = {k: v for k, v in overlaps.items()
                  if set(v) != {"fl_get_piano_roll_info", "fl_get_piano_roll_state"}}
    assert not unexpected, f"near-duplicate tools registered: {unexpected}"


def test_channel_info_parameter_name_matches_client_expectation():
    """fl_get_channel_info must accept `channel` (the name every caller uses)."""
    names = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    tool = names["fl_get_channel_info"]
    props = tool.parameters.get("properties", {})
    assert "channel" in props, (
        "fl_get_channel_info still declares `index`; MCP clients naturally send "
        "`channel`, which fails schema validation. Rename the parameter or add an "
        "alias at the server layer."
    )
