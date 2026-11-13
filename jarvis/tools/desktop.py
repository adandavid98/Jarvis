"""Windows desktop, OS, shell, and file system tools."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from jarvis.config import JarvisConfig
from jarvis.tools._paths import resolve_workspace_path
from jarvis.tools.registry import ToolRegistry, ToolSpec

_DANGEROUS_PATTERNS = [
    re.compile(r"\b(format-volume|diskpart|stop-computer|restart-computer|shutdown)\b", re.I),
    re.compile(r"\b(remove-item|rm|del|erase|rmdir|rd)\b.*(-recurse|-force|/s|/q)", re.I),
    re.compile(r"\breg\s+delete\b", re.I),
    re.compile(r"\b(bcdedit|cipher\s+/w)\b", re.I),
]

_WINDOWS_APP_ALIASES: dict[str, list[str]] = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "calc": ["calc.exe"],
    "explorer": ["explorer.exe"],
    "files": ["explorer.exe"],
    "terminal": ["wt.exe"],
    "wt": ["wt.exe"],
    "powershell": ["powershell.exe"],
    "cmd": ["cmd.exe"],
    "vscode": ["code"],
    "code": ["code"],
    "chrome": ["chrome"],
    "edge": ["msedge"],
    "msedge": ["msedge"],
    "task manager": ["taskmgr.exe"],
    "settings": ["ms-settings:"],
}

_MAX_OUTPUT_CHARS = 6000


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _is_dangerous_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in _DANGEROUS_PATTERNS)


def register_desktop_tools(registry: ToolRegistry, config: JarvisConfig) -> None:
    """Register Windows desktop, shell, and file management tools."""

    def get_system_status() -> dict[str, Any]:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(config.workspace_dir))
        battery = psutil.sensors_battery()
        return {
            "status": "ok",
            "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "os": f"{platform.system()} {platform.release()}",
            "hostname": platform.node(),
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "memory_used_percent": vm.percent,
            "memory_available_gb": round(vm.available / (1024**3), 2),
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "battery": (
                {
                    "percent": round(battery.percent, 1),
                    "plugged_in": bool(battery.power_plugged),
                }
                if battery is not None
                else None
            ),
            "workspace_dir": str(config.workspace_dir),
        }

    def open_application_or_url(target: str) -> dict[str, Any]:
        cleaned = target.strip()
        if not cleaned:
            return {"status": "error", "error": "Target cannot be empty."}

        if cleaned.lower().startswith(("http://", "https://")):
            if os.environ.get("JARVIS_EMBEDDED_VIEWER") != "1":
                webbrowser.open(cleaned)
            return {
                "status": "ok",
                "opened": cleaned,
                "kind": "url",
                "viewer_url": cleaned,
                "title": cleaned,
            }

        alias_cmd = _WINDOWS_APP_ALIASES.get(cleaned.lower())
        if alias_cmd:
            if alias_cmd[0].endswith(":"):
                os.startfile(alias_cmd[0])  # type: ignore[attr-defined]
            else:
                subprocess.Popen(alias_cmd, shell=False)
            return {"status": "ok", "opened": cleaned, "command": alias_cmd}

        # Try resolving as a workspace file or local executable/path
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            ws_candidate = config.workspace_dir / candidate
            if ws_candidate.exists():
                candidate = ws_candidate

        if candidate.exists():
            os.startfile(str(candidate))  # type: ignore[attr-defined]
            return {"status": "ok", "opened": str(candidate), "kind": "path"}

        subprocess.Popen([cleaned], shell=False)
        return {"status": "ok", "opened": cleaned, "kind": "executable"}

    def run_shell_command(
        command: str,
        timeout_sec: int = 20,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        if not command.strip():
            return {"status": "error", "error": "Command cannot be empty."}

        if _is_dangerous_command(command) and not (confirmed or config.auto_approve_dangerous):
            return {
                "status": "needs_confirmation",
                "command": command,
                "message": (
                    "This command appears potentially destructive. Ask the user for "
                    "explicit verbal confirmation, then re-run with confirmed=true."
                ),
            }

        clamped_timeout = max(1, min(int(timeout_sec), 120))
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            cwd=str(config.workspace_dir),
            capture_output=True,
            text=True,
            timeout=clamped_timeout,
        )
        return {
            "status": "ok" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout": _truncate(completed.stdout.strip()),
            "stderr": _truncate(completed.stderr.strip()),
        }

    def list_directory(path: str = ".", show_hidden: bool = False) -> dict[str, Any]:
        target = resolve_workspace_path(config.workspace_dir, path, allow_outside=True)
        if not target.is_dir():
            return {"status": "error", "error": f"Not a directory: {target}"}

        entries: list[dict[str, Any]] = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if not show_hidden and item.name.startswith("."):
                continue
            entries.append(
                {
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size_bytes": item.stat().st_size if item.is_file() else None,
                }
            )
            if len(entries) >= 100:
                break
        return {"status": "ok", "path": str(target), "entries": entries}

    def read_file(path: str, start_line: int = 1, max_lines: int = 120) -> dict[str, Any]:
        target = resolve_workspace_path(config.workspace_dir, path, allow_outside=True)
        if not target.is_file():
            return {"status": "error", "error": f"File not found: {target}"}

        start = max(1, int(start_line))
        limit = max(1, min(int(max_lines), 400))
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start - 1 : start - 1 + limit]
        return {
            "status": "ok",
            "path": str(target),
            "start_line": start,
            "total_lines": len(lines),
            "content": _truncate("\n".join(selected)),
        }

    def write_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
        target = resolve_workspace_path(
            config.workspace_dir,
            path,
            allow_outside=config.auto_approve_dangerous,
        )
        if target.exists() and not overwrite:
            return {
                "status": "needs_confirmation",
                "path": str(target),
                "message": "File already exists. Ask user to confirm overwriting, then pass overwrite=true.",
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "status": "ok",
            "path": str(target),
            "bytes_written": len(content.encode("utf-8")),
        }

    def web_search(query: str) -> dict[str, Any]:
        cleaned = query.strip()
        if not cleaned:
            return {"status": "error", "error": "Search query cannot be empty."}
        search_url = f"https://duckduckgo.com/?q={urllib.parse.quote_plus(cleaned)}"
        if os.environ.get("JARVIS_EMBEDDED_VIEWER") != "1":
            webbrowser.open(search_url)
        return {
            "status": "ok",
            "query": cleaned,
            "kind": "web_search",
            "viewer_url": search_url,
            "title": f"Search: {cleaned}",
        }

    def enter_standby() -> dict[str, Any]:
        return {"status": "ok", "standby": True, "message": "Returning to idle wake-word standby."}

    registry.register(
        ToolSpec(
            name="get_system_status",
            description="Get current Windows CPU, memory, disk, battery, and local time.",
            parameters={"type": "OBJECT", "properties": {}},
            handler=get_system_status,
        )
    )
    registry.register(
        ToolSpec(
            name="open_application_or_url",
            description="Open a Windows application (e.g. notepad, calculator, vscode, chrome, terminal), file, or HTTPS URL.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "target": {
                        "type": "STRING",
                        "description": "App name, executable, local path, or https:// URL.",
                    }
                },
                "required": ["target"],
            },
            handler=open_application_or_url,
        )
    )
    registry.register(
        ToolSpec(
            name="web_search",
            description="Search the web for a query and display the live results inside Jarvis's integrated viewer.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Search query to look up on the web.",
                    }
                },
                "required": ["query"],
            },
            handler=web_search,
        )
    )
    registry.register(
        ToolSpec(
            name="enter_standby",
            description="Return Jarvis to idle wake-word standby (listening only for 'Hey Jarvis') when the user says goodbye, 'that is all', 'standby', or 'go to sleep'.",
            parameters={"type": "OBJECT", "properties": {}},
            handler=enter_standby,
        )
    )
    registry.register(
        ToolSpec(
            name="run_shell_command",
            description="Run a PowerShell command in the workspace directory and return stdout/stderr.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "command": {"type": "STRING", "description": "PowerShell command to execute."},
                    "timeout_sec": {"type": "INTEGER", "description": "Timeout in seconds (1-120)."},
                    "confirmed": {
                        "type": "BOOLEAN",
                        "description": "Set true only after explicit user confirmation for destructive commands.",
                    },
                },
                "required": ["command"],
            },
            handler=run_shell_command,
        )
    )
    registry.register(
        ToolSpec(
            name="list_directory",
            description="List files and subdirectories at a given path (defaults to workspace root).",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Relative or absolute directory path."},
                    "show_hidden": {"type": "BOOLEAN"},
                },
            },
            handler=list_directory,
        )
    )
    registry.register(
        ToolSpec(
            name="read_file",
            description="Read lines from a text or source code file.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Relative or absolute file path."},
                    "start_line": {"type": "INTEGER"},
                    "max_lines": {"type": "INTEGER"},
                },
                "required": ["path"],
            },
            handler=read_file,
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description="Create or update a file inside the workspace directory.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Path inside the workspace."},
                    "content": {"type": "STRING", "description": "Full text content to write."},
                    "overwrite": {"type": "BOOLEAN", "description": "Must be true to overwrite an existing file."},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        )
    )
