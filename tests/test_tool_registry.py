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


def test_channel_tools_use_consistent_parameter_name():
    """Every tool named fl_*channel* must declare its channel index as `channel`.

    Regression: 8 tools used `index` and one used `channel_index` while the
    rest used `channel`. MCP clients infer the argument name from the tool
    name, so they sent `channel` and Pydantic rejected the call before the
    tool body ran (additionalProperties=false).
    """
    tools = asyncio.run(srv.mcp.list_tools())
    offenders = {}
    for tool in tools:
        if "channel" not in tool.name.lower():
            continue
        props = tool.parameters.get("properties", {})
        if not any(p in props for p in ("index", "channel", "channel_index")):
            continue  # e.g. fl_get_channel_count has no channel argument
        if "channel" not in props:
            offenders[tool.name] = [p for p in ("index", "channel_index") if p in props]
    assert not offenders, f"channel tools without a `channel` parameter: {offenders}"


def test_documented_tool_count_matches_registry():
    """README counts must track the registry after tools are added or removed."""
    count = len(asyncio.run(srv.mcp.list_tools()))
    for name in ("README.md", "README_CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"\b%d\b" % count, text), (
            f"{name} does not mention the current tool count ({count})"
        )
