"""Multi-step autonomous software engineering agent tool for Jarvis."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types

from jarvis.config import JarvisConfig
from jarvis.tools.registry import ToolRegistry, ToolSpec

_AGENT_ALLOWED_TOOLS = (
    "list_directory",
    "read_file",
    "write_file",
    "edit_file",
    "search_code",
    "run_tests",
    "run_shell_command",
)

_CODING_MODELS = ("gemini-2.5-pro", "gemini-2.5-flash")

_CODER_SYSTEM_PROMPT = """You are Jarvis's senior software engineer subagent operating in the user's active Windows workspace.
Plan and execute the requested coding task completely:
1. Inspect existing files first (`list_directory`, `search_code`, `read_file`) before writing or modifying code.
2. Write clean, modular, production-quality code using `write_file` and `edit_file`. When creating files for a requested project, pass `overwrite=true` if updating existing files.
3. Verify your work by running `run_tests` or `run_shell_command` when appropriate, and fix any failures.
4. Keep responses concise: conclude with a short bullet summary of files created/modified and verification results."""


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
        max_steps: int = 6,
    ) -> dict[str, Any]:
        cleaned_task = task.strip()
        if not cleaned_task:
            return {"status": "error", "error": "Coding task cannot be empty."}
        if not config.gemini_api_key:
            return {"status": "error", "error": "GEMINI_API_KEY is not configured."}

        step_limit = max(1, min(10, int(max_steps)))
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
            temperature=0.2,
        )

        prompt_text = (
            f"Workspace root: {config.workspace_dir}\n"
            f"Target directory hint: {target_subdir.strip() or '.'}\n\n"
            f"Task:\n{cleaned_task}"
        )
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
        ]

        files_changed: list[str] = []
        used_model = _CODING_MODELS[0]
        steps_taken = 0
        final_summary = ""

        for step in range(step_limit):
            steps_taken = step + 1
            response = None
            last_exc: Exception | None = None
            for model_name in _CODING_MODELS:
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
                if on_step is not None:
                    await on_step(f"{call.name}: {call_args.get('path') or call_args.get('command') or ''}".strip())
                fn_response_parts.append(
                    types.Part.from_function_response(name=call.name, response=result)
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
            "summary": final_summary,
        }

    registry.register(
        ToolSpec(
            name="run_coding_agent",
            description=(
                "Autonomous multi-step software engineering agent for building complete coding projects, "
                "creating/editing multiple source files, and running/fixing tests inside the active workspace."
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
                        "description": "Safety cap on agent reasoning/tool cycles (default 6, max 10).",
                    },
                },
                "required": ["task"],
            },
            handler=run_coding_agent,
        )
    )
