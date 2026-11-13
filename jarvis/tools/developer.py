"""Coding and developer workflow tools."""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path
from typing import Any

from jarvis.config import JarvisConfig
from jarvis.tools._paths import resolve_workspace_path
from jarvis.tools.registry import ToolRegistry, ToolSpec

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", "dist", "build"}


def register_developer_tools(registry: ToolRegistry, config: JarvisConfig) -> None:
    """Register code search, git inspection, and test runner tools."""

    def search_code(
        query: str,
        glob_pattern: str = "*",
        subdir: str = ".",
        max_results: int = 25,
    ) -> dict[str, Any]:
        needle = query.strip()
        if not needle:
            return {"status": "error", "error": "Query cannot be empty."}

        root = resolve_workspace_path(config.workspace_dir, subdir, allow_outside=True)
        if not root.is_dir():
            return {"status": "error", "error": f"Directory not found: {root}"}

        matches: list[dict[str, Any]] = []
        needle_lower = needle.lower()
        cap = max(1, min(int(max_results), 100))

        for path in root.rglob("*"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file() or not fnmatch.fnmatch(path.name, glob_pattern):
                continue
            if path.stat().st_size > 512_000:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle_lower in line.lower():
                    matches.append(
                        {
                            "file": str(path.relative_to(root)),
                            "line": lineno,
                            "text": line.strip()[:200],
                        }
                    )
                    if len(matches) >= cap:
                        return {"status": "ok", "truncated": True, "matches": matches}

        return {"status": "ok", "truncated": False, "matches": matches}

    def git_inspect(action: str = "status", max_commits: int = 5) -> dict[str, Any]:
        act = action.strip().lower()
        if act == "status":
            cmd = ["git", "status", "--short", "--branch"]
        elif act == "diff":
            cmd = ["git", "diff", "--stat"]
        elif act == "log":
            n = max(1, min(int(max_commits), 20))
            cmd = ["git", "log", f"-n{n}", "--oneline"]
        else:
            return {"status": "error", "error": f"Unsupported git action: {action}"}

        completed = subprocess.run(
            cmd,
            cwd=str(config.workspace_dir),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {
            "status": "ok" if completed.returncode == 0 else "failed",
            "action": act,
            "output": (completed.stdout or completed.stderr).strip()[:5000],
        }

    def run_tests(target: str = "") -> dict[str, Any]:
        cmd = [sys.executable, "-m", "pytest", "-q"]
        if target.strip():
            resolved = resolve_workspace_path(config.workspace_dir, target.strip(), allow_outside=False)
            cmd.append(str(resolved))

        completed = subprocess.run(
            cmd,
            cwd=str(config.workspace_dir),
            capture_output=True,
            text=True,
            timeout=90,
        )
        combined = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
        return {
            "status": "ok" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "output": combined[-5000:],
        }

    registry.register(
        ToolSpec(
            name="search_code",
            description="Search source files in the workspace for a case-insensitive text query.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING", "description": "Text to search for."},
                    "glob_pattern": {"type": "STRING", "description": "Filename pattern such as '*.py' or '*'."},
                    "subdir": {"type": "STRING", "description": "Subdirectory to search inside."},
                    "max_results": {"type": "INTEGER"},
                },
                "required": ["query"],
            },
            handler=search_code,
        )
    )
    registry.register(
        ToolSpec(
            name="git_inspect",
            description="Inspect git status, diff summary, or recent commit log in the workspace.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "action": {"type": "STRING", "enum": ["status", "diff", "log"]},
                    "max_commits": {"type": "INTEGER"},
                },
            },
            handler=git_inspect,
        )
    )
    registry.register(
        ToolSpec(
            name="run_tests",
            description="Run pytest on the workspace or a specific test file/directory.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "target": {"type": "STRING", "description": "Optional test file or folder path."},
                },
            },
            handler=run_tests,
        )
    )
