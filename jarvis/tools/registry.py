"""Async-safe tool registry and Gemini FunctionDeclaration adapter."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable

from google.genai import types

from jarvis.memory import JarvisMemory


@dataclass(slots=True)
class ToolSpec:
    """Metadata and callable handler for a single Jarvis automation tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]


class ToolRegistry:
    """Registers tools, exports Gemini Live declarations, and dispatches calls."""

    def __init__(self, memory: JarvisMemory | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._memory = memory

    def register(self, spec: ToolSpec) -> None:
        """Register a tool specification."""
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool registered: {spec.name}")
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        """Return registered tool names in registration order."""
        return list(self._tools.keys())

    def to_gemini_tool(self) -> types.Tool:
        """Convert all registered tools into a Gemini `types.Tool` with NON_BLOCKING behavior."""
        declarations = [
            types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                behavior="NON_BLOCKING",
                parameters=spec.parameters,
            )
            for spec in self._tools.values()
        ]
        return types.Tool(function_declarations=declarations)

    async def execute(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Execute a registered tool without blocking the asyncio event loop."""
        spec = self._tools.get(name)
        safe_args = dict(args or {})
        args_json = json.dumps(safe_args, default=str)

        if spec is None:
            result = {"status": "error", "error": f"Unknown tool: {name}"}
            if self._memory is not None:
                self._memory.log_tool_call(name, args_json, "unknown_tool")
            return result

        try:
            if inspect.iscoroutinefunction(spec.handler):
                output = await spec.handler(**safe_args)
            else:
                output = await asyncio.to_thread(spec.handler, **safe_args)

            result = output if isinstance(output, dict) else {"status": "ok", "result": output}
            if self._memory is not None:
                self._memory.log_tool_call(name, args_json, str(result.get("status", "ok")))
            return result
        except TypeError as exc:
            result = {"status": "error", "error": f"Invalid arguments for {name}: {exc}"}
            if self._memory is not None:
                self._memory.log_tool_call(name, args_json, "invalid_args")
            return result
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            if self._memory is not None:
                self._memory.log_tool_call(name, args_json, "error")
            return result
