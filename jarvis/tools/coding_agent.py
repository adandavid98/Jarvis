"""Multi-step autonomous software engineering agent tool for Jarvis."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types

from jarvis.config import JarvisConfig
from jarvis.tools._paths import resolve_workspace_path
from jarvis.tools.registry import ToolRegistry, ToolSpec

_AGENT_ALLOWED_TOOLS = (
    "list_directory",
    "search_code",
    "read_file",
    "edit_file",
    "write_file",
    "git_inspect",
    "run_tests",
    "run_shell_command",
)

_DEEP_MODELS = ("gemini-2.5-pro", "gemini-2.5-flash")
_FAST_MODELS = ("gemini-2.5-flash", "gemini-2.5-pro")
_MAX_TOOL_RESPONSE_CHARS = 6000

_CODER_SYSTEM_PROMPT = """You are Jarvis's senior software engineer subagent operating in the user's active Windows workspace.
Follow a disciplined, token-efficient engineering workflow:
1. Locate before reading: use `search_code` and `git_inspect` first to find exact files, symbols, and line numbers before calling `read_file`.
2. Read economically: pass `start_line` and `max_lines` (<= 120) to `read_file` around only the relevant block; never re-read unchanged ranges you already saw.
3. Batch independent tool calls together in parallel (e.g., multiple `read_file` or `search_code` calls in one turn).
4. Prefer targeted `edit_file` over rewriting whole files; preserve unrelated comments and match existing repo patterns/helpers without duplicating logic.
5. Verify after your final edit using `run_tests` (or targeted `run_shell_command` checks) and fix any reported errors before concluding.
6. Ground every claim strictly in observed tool output — cite only file paths, symbols, and test results you directly saw, and end with a concise bullet summary."""


def _workspace_snapshot(workspace_dir: Path, target_subdir: str) -> str:
    """Return a compact top-level listing so the agent skips an initial directory-discovery round trip."""
    try:
        root = resolve_workspace_path(workspace_dir, target_subdir or ".", allow_outside=True)
        if not root.is_dir():
            return "(target directory does not exist yet)"
        skip = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", "dist", "build"}
        entries: list[str] = []
        for entry in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name in skip or entry.name.startswith("."):
                continue
            entries.append(f"{entry.name}/" if entry.is_dir() else entry.name)
            if len(entries) >= 35:
                entries.append("...")
                break
        return ", ".join(entries) if entries else "(empty directory)"
    except Exception:
        return "(unavailable)"


def _compact_tool_response(result: dict[str, Any]) -> dict[str, Any]:
    """Trim oversized text payloads before appending to model context to cap token growth."""
    compacted: dict[str, Any] = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > _MAX_TOOL_RESPONSE_CHARS:
            half = _MAX_TOOL_RESPONSE_CHARS // 2
            omitted = len(v) - _MAX_TOOL_RESPONSE_CHARS
            compacted[k] = f"{v[:half]}\n...[truncated {omitted} chars for token efficiency]...\n{v[-half:]}"
        else:
            compacted[k] = v
    return compacted


def _select_models(task: str, mode: str) -> tuple[str, ...]:
    """Route straightforward edits to Flash and complex multi-file debugging/architecture to Pro."""
    cleaned = mode.strip().lower()
    if cleaned == "fast":
        return _FAST_MODELS
    if cleaned == "deep":
        return _DEEP_MODELS
    low = task.lower()
    deep_keywords = (
        "refactor", "architect", "debug", "fix bug", "error", "trace", "test", "pytest",
        "multi", "migrat", "optimiz", "security", "audit", "integrat", "api", "async",
    )
    if len(task) >= 140 or any(kw in low for kw in deep_keywords):
        return _DEEP_MODELS
    return _FAST_MODELS


def register_coding_agent_tool(
    registry: ToolRegistry,
    config: JarvisConfig,
    *,
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Register `run_coding_agent` for multi-file software projects and debugging."""

    async def run_coding_agent(
        task: str,
        target_subdir: str = ".",
        max_steps: int = 8,
        mode: str = "auto",
    ) -> dict[str, Any]:
        cleaned_task = task.strip()
        if not cleaned_task:
            return {"status": "error", "error": "Coding task cannot be empty."}
        if not config.gemini_api_key:
            return {"status": "error", "error": "GEMINI_API_KEY is not configured."}

        step_limit = max(1, min(12, int(max_steps)))
        declarations: list[types.FunctionDeclaration] = []
        for tool_name in _AGENT_ALLOWED_TOOLS:
            spec = registry._tools.get(tool_name)  # noqa: SLF001
            if spec is not None:
                declarations.append(
                    types.FunctionDeclaration(
                        name=spec.name,
                        description=spec.description,
                        parameters=spec.parameters,
                    )
                )

        if not declarations:
            return {"status": "error", "error": "Workspace developer tools are not registered."}

        client = genai.Client(api_key=config.gemini_api_key)
        gen_config = types.GenerateContentConfig(
            system_instruction=_CODER_SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=declarations)],
            temperature=0.15,
        )

        subdir = target_subdir.strip() or "."
        snapshot = _workspace_snapshot(config.workspace_dir, subdir)
        prompt_text = (
            f"Workspace root: {config.workspace_dir}\n"
            f"Target directory hint: {subdir}\n"
            f"Top-level entries: {snapshot}\n\n"
            f"Task:\n{cleaned_task}"
        )
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
        ]

        model_order = _select_models(cleaned_task, mode)
        files_changed: list[str] = []
        checks_run: list[str] = []
        used_model = model_order[0]
        steps_taken = 0
        final_summary = ""

        for step in range(step_limit):
            steps_taken = step + 1
            response = None
            last_exc: Exception | None = None
            for model_name in model_order:
                try:
                    response = await client.aio.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=gen_config,
                    )
                    used_model = model_name
                    break
                except Exception as exc:
                    last_exc = exc
                    continue

            if response is None:
                return {
                    "status": "error",
                    "error": f"Coding model request failed: {last_exc}",
                    "steps": steps_taken,
                    "files_changed": files_changed,
                }

            candidate = response.candidates[0] if response.candidates else None
            if candidate and candidate.content:
                contents.append(candidate.content)

            fn_calls = getattr(response, "function_calls", None) or []
            if not fn_calls:
                final_summary = (getattr(response, "text", None) or "").strip()
                break

            fn_response_parts: list[types.Part] = []
            for call in fn_calls:
                call_args = dict(call.args or {})
                if call.name in {"write_file", "create_document"} and "overwrite" not in call_args:
                    call_args["overwrite"] = True
                result = await registry.execute(call.name, call_args)
                if call.name in {"write_file", "edit_file"} and result.get("status") == "ok":
                    changed_path = str(result.get("path") or call_args.get("path") or "")
                    if changed_path and changed_path not in files_changed:
                        files_changed.append(changed_path)
                elif call.name == "run_tests":
                    checks_run.append(f"run_tests:{result.get('status', 'unknown')}")
                elif call.name == "run_shell_command":
                    checks_run.append(
                        f"shell({str(call_args.get('command', ''))[:40]}):{result.get('status', 'unknown')}"
                    )
                if on_step is not None:
                    await on_step(f"{call.name}: {call_args.get('path') or call_args.get('command') or ''}".strip())
                fn_response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response=_compact_tool_response(result),
                    )
                )

            contents.append(types.Content(role="user", parts=fn_response_parts))

        if not final_summary:
            final_summary = (
                f"Completed {steps_taken} engineering steps. Modified/created {len(files_changed)} file(s)."
            )

        return {
            "status": "ok",
            "model": used_model,
            "steps": steps_taken,
            "files_changed": files_changed,
            "checks_run": checks_run,
            "summary": final_summary,
        }

    registry.register(
        ToolSpec(
            name="run_coding_agent",
            description=(
                "Autonomous multi-step software engineering agent for building complete coding projects, "
                "inspecting git/code, making surgical edits, and running/fixing tests inside the active workspace."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "task": {
                        "type": "STRING",
                        "description": "Detailed specification of the coding project, feature, or bug fix to build.",
                    },
                    "target_subdir": {
                        "type": "STRING",
                        "description": "Optional relative subdirectory in the workspace (default '.').",
                    },
                    "max_steps": {
                        "type": "INTEGER",
                        "description": "Safety cap on agent reasoning/tool cycles (default 8, max 12).",
                    },
                    "mode": {
                        "type": "STRING",
                        "enum": ["auto", "fast", "deep"],
                        "description": "Model routing: 'auto' (default), 'fast' (Flash), or 'deep' (Pro).",
                    },
                },
                "required": ["task"],
            },
            handler=run_coding_agent,
        )
    )
