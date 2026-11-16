"""Tool registration entrypoint for Jarvis."""

from __future__ import annotations

from jarvis.config import JarvisConfig
from jarvis.memory import JarvisMemory
from jarvis.tools.coding_agent import register_coding_agent_tool
from jarvis.tools.desktop import register_desktop_tools
from jarvis.tools.developer import register_developer_tools
from jarvis.tools.productivity import register_productivity_tools
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.research import register_research_tools


def build_default_registry(config: JarvisConfig, memory: JarvisMemory) -> ToolRegistry:
    """Create and populate the default Jarvis tool registry."""
    registry = ToolRegistry(memory=memory)
    register_desktop_tools(registry, config)
    register_productivity_tools(registry, config, memory)
    register_developer_tools(registry, config)
    register_research_tools(registry, config)
    register_coding_agent_tool(registry, config)
    return registry
