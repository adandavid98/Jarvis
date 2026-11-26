"""Windows desktop, OS, shell, and file system tools."""

from __future__ import annotations

import ctypes
import html
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from jarvis.config import JarvisConfig
from jarvis.tools._paths import resolve_workspace_path
from jarvis.tools.documents import generate_document_file
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
    "google chrome": ["chrome"],
    "edge": ["msedge"],
    "msedge": ["msedge"],
    "task manager": ["taskmgr.exe"],
    "settings": ["ms-settings:"],
}

_MAX_OUTPUT_CHARS = 6000


def _find_chrome_executable() -> str | None:
    """Locate Google Chrome executable on Windows even when not on PATH."""
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if not base:
            continue
        candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _win_get_clipboard_text() -> str | None:
    """Read current Unicode text from the Windows clipboard without spawning PowerShell."""
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(0):
        return None
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _win_set_clipboard_text(text: str) -> bool:
    """Write Unicode text to the Windows clipboard."""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    data = (text + "\x00").encode("utf-16-le")
    if not user32.OpenClipboard(0):
        return False
    try:
        user32.EmptyClipboard()
        hmem = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not hmem:
            return False
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(hmem)
        return bool(user32.SetClipboardData(13, hmem))
    finally:
        user32.CloseClipboard()


def _find_visible_chrome_hwnd() -> int | None:
    """Return the top-most visible Google Chrome window handle on Windows, if any."""
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum_cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.lower()
        if title == "google chrome" or " - google chrome" in title:
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(_enum_cb, 0)
    return found[0] if found else None


def _send_win_key_combo(*vk_codes: int) -> None:
    user32 = ctypes.windll.user32
    keyeventf_keyup = 0x0002
    for vk in vk_codes:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vk_codes):
        user32.keybd_event(vk, 0, keyeventf_keyup, 0)


def _navigate_chrome_same_tab(url: str) -> bool:
    """Focus the active Google Chrome window and navigate its current tab via Ctrl+L / Ctrl+V / Enter."""
    if os.name != "nt":
        return False
    hwnd = _find_visible_chrome_hwnd()
    if not hwnd:
        return False
    prev_clip = _win_get_clipboard_text()
    if not _win_set_clipboard_text(url):
        return False
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.08)
        _send_win_key_combo(0x11, 0x4C)  # Ctrl+L
        time.sleep(0.05)
        _send_win_key_combo(0x11, 0x56)  # Ctrl+V
        time.sleep(0.04)
        _send_win_key_combo(0x0D)  # Enter
        time.sleep(0.08)
        return True
    except Exception:
        return False
    finally:
        if prev_clip is not None:
            _win_set_clipboard_text(prev_clip)


def _open_url_external(url: str, browser: str = "default", *, same_tab: bool = False) -> str:
    """Open `url` in Google Chrome (optionally reusing its active tab) or the OS default browser."""
    pref = browser.strip().lower()
    if pref in {"chrome", "google chrome"}:
        if same_tab and _navigate_chrome_same_tab(url):
            return "chrome_same_tab"
        chrome_exe = _find_chrome_executable()
        if chrome_exe:
            subprocess.Popen([chrome_exe, url], shell=False)
            return "chrome"
        if os.name == "nt":
            try:
                subprocess.Popen(["cmd", "/c", "start", "", "chrome", url], shell=False)
                return "chrome"
            except Exception:
                pass
    webbrowser.open(url)
    return "default_browser"


def _is_external_browser_requested(browser: str) -> bool:
    return browser.strip().lower() in {"chrome", "google chrome", "external", "default"}


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

    def open_application_or_url(
        target: str,
        browser: str = "viewer",
        same_tab: bool = False,
    ) -> dict[str, Any]:
        cleaned = target.strip()
        if not cleaned:
            return {"status": "error", "error": "Target cannot be empty."}

        if cleaned.lower().startswith(("http://", "https://")):
            force_external = _is_external_browser_requested(browser)
            if force_external or os.environ.get("JARVIS_EMBEDDED_VIEWER") != "1":
                opened_in = _open_url_external(cleaned, browser=browser, same_tab=bool(same_tab))
                res: dict[str, Any] = {
                    "status": "ok",
                    "opened": cleaned,
                    "kind": "url",
                    "opened_in": opened_in,
                    "title": cleaned,
                }
                if not force_external:
                    res["viewer_url"] = cleaned
                return res
            return {
                "status": "ok",
                "opened": cleaned,
                "kind": "url",
                "opened_in": "embedded_viewer",
                "viewer_url": cleaned,
                "title": cleaned,
            }

        alias_cmd = _WINDOWS_APP_ALIASES.get(cleaned.lower())
        if alias_cmd:
            if cleaned.lower() in {"chrome", "google chrome"}:
                chrome_exe = _find_chrome_executable()
                if chrome_exe:
                    subprocess.Popen([chrome_exe], shell=False)
                    return {"status": "ok", "opened": cleaned, "command": [chrome_exe]}
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
        if target.exists() and not (overwrite or config.auto_approve_dangerous):
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

    def edit_file(path: str, target_text: str, replacement: str) -> dict[str, Any]:
        target = resolve_workspace_path(
            config.workspace_dir,
            path,
            allow_outside=config.auto_approve_dangerous,
        )
        if not target.is_file():
            return {"status": "error", "error": f"File not found: {target}"}
        original = target.read_text(encoding="utf-8", errors="replace")
        if target_text not in original:
            return {"status": "error", "error": "Target text not found in file."}
        updated = original.replace(target_text, replacement, 1)
        target.write_text(updated, encoding="utf-8")
        return {"status": "ok", "path": str(target), "bytes_written": len(updated.encode("utf-8"))}

    def create_document(
        path: str,
        title: str = "",
        content: str = "",
        overwrite: bool = False,
        style: str = "standard",
        subtitle: str = "",
        author: str = "",
        institution: str = "",
        course: str = "",
        date: str = "",
        references: list[str] | None = None,
    ) -> dict[str, Any]:
        target = resolve_workspace_path(
            config.workspace_dir,
            path,
            allow_outside=config.auto_approve_dangerous,
        )
        if target.exists() and not (overwrite or config.auto_approve_dangerous):
            return {
                "status": "needs_confirmation",
                "path": str(target),
                "message": "File already exists. Ask user to confirm overwriting, then pass overwrite=true.",
            }

        return generate_document_file(
            target,
            content=content,
            title=title,
            subtitle=subtitle,
            author=author,
            institution=institution,
            course=course,
            date=date,
            style=style,
            references=references,
        )

    _TAG_RE = re.compile(r"<[^>]+>")
    _DDG_LINK_RE = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    _DDG_SNIPPET_RE = re.compile(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.I | re.S)

    def _clean_html_text(raw_html: str, limit: int) -> str:
        plain = " ".join(html.unescape(_TAG_RE.sub(" ", raw_html)).split())
        if len(plain) <= limit:
            return plain
        return plain[: max(0, limit - 3)].rstrip() + "..."

    def _decode_ddg_href(href: str) -> str:
        unescaped = html.unescape(href.strip())
        parsed = urllib.parse.urlparse(unescaped)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("uddg"):
                return qs["uddg"][0]
        if unescaped.startswith("//"):
            return "https:" + unescaped
        return unescaped

    async def _fetch_web_snippets(query: str, max_results: int = 3) -> list[dict[str, str]]:
        """Fetch up to 3 tiny web snippets (~<700 chars total) with strict timeout & fallback."""
        snippets: list[dict[str, str]] = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(timeout=4.5, follow_redirects=True, headers=headers) as client:
                resp = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                )
                if resp.status_code == 200 and resp.text:
                    titles = _DDG_LINK_RE.findall(resp.text)
                    bodies = _DDG_SNIPPET_RE.findall(resp.text)
                    for idx, (href, raw_title) in enumerate(titles[:max_results]):
                        title_txt = _clean_html_text(raw_title, 75)
                        snip_txt = _clean_html_text(bodies[idx], 150) if idx < len(bodies) else ""
                        if title_txt or snip_txt:
                            snippets.append(
                                {
                                    "title": title_txt,
                                    "url": _decode_ddg_href(href)[:160],
                                    "snippet": snip_txt,
                                }
                            )
                if not snippets:
                    ia = await client.get(
                        "https://api.duckduckgo.com/",
                        params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                    )
                    if ia.status_code == 200:
                        data = ia.json()
                        abstract = str(data.get("AbstractText") or "").strip()
                        if abstract:
                            snippets.append(
                                {
                                    "title": str(data.get("Heading") or query)[:75],
                                    "url": str(data.get("AbstractURL") or "")[:160],
                                    "snippet": _clean_html_text(abstract, 180),
                                }
                            )
                        for topic in (data.get("RelatedTopics") or [])[:max_results]:
                            if len(snippets) >= max_results:
                                break
                            if isinstance(topic, dict) and topic.get("Text"):
                                snippets.append(
                                    {
                                        "title": _clean_html_text(str(topic["Text"]), 70),
                                        "url": str(topic.get("FirstURL") or "")[:160],
                                        "snippet": _clean_html_text(str(topic["Text"]), 140),
                                    }
                                )
        except Exception:
            return []
        return snippets[:max_results]

    _WEATHER_QUERY_RE = re.compile(r"\b(weather|temperature|temperatura|clima|forecast|pron[oó]stico|grados)\b", re.I)
    _WEATHER_STOP_RE = re.compile(
        r"\b(current|today|now|right|what|is|the|in|at|for|of|time|and|weather|temperature|temperatura|clima|forecast|pronostico|pronóstico|actual|hoy|en|de|la|el|cual|cu[aá]l|como|c[oó]mo|esta|est[aá])\b",
        re.I,
    )

    async def _fetch_live_weather_snippet(query: str) -> list[dict[str, str]]:
        """Fetch real-time temperature/weather from Open-Meteo when query asks for weather."""
        if not _WEATHER_QUERY_RE.search(query):
            return []
        city = " ".join(_WEATHER_STOP_RE.sub(" ", query).split()).strip(" ?,.")
        if len(city) < 2:
            city = "New York"
        try:
            async with httpx.AsyncClient(timeout=4.5, follow_redirects=True) as client:
                geo = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city, "count": 1, "language": "en", "format": "json"},
                )
                if geo.status_code != 200:
                    return []
                results = geo.json().get("results") or []
                if not results:
                    return []
                loc = results[0]
                lat, lon = loc["latitude"], loc["longitude"]
                label = ", ".join(p for p in (loc.get("name"), loc.get("admin1"), loc.get("country")) if p)
                fc = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m",
                        "temperature_unit": "celsius",
                    },
                )
                if fc.status_code != 200:
                    return []
                cur = fc.json().get("current") or {}
                temp_c = cur.get("temperature_2m")
                if temp_c is None:
                    return []
                temp_f = round(float(temp_c) * 9 / 5 + 32, 1)
                feels_c = cur.get("apparent_temperature")
                hum = cur.get("relative_humidity_2m")
                wind = cur.get("wind_speed_10m")
                summary = (
                    f"Live weather in {label}: {temp_c}°C ({temp_f}°F), "
                    f"feels like {feels_c}°C, humidity {hum}%, wind {wind} km/h."
                )
                return [
                    {
                        "title": f"Current Weather — {label}",
                        "url": f"https://open-meteo.com/",
                        "snippet": summary,
                    }
                ]
        except Exception:
            return []

    async def web_search(
        query: str,
        browser: str = "viewer",
        same_tab: bool = False,
    ) -> dict[str, Any]:
        cleaned = query.strip()
        if not cleaned:
            return {"status": "error", "error": "Search query cannot be empty."}
        force_external = _is_external_browser_requested(browser)
        pref = browser.strip().lower()
        encoded = urllib.parse.quote_plus(cleaned)
        search_url = (
            f"https://www.google.com/search?q={encoded}"
            if pref in {"chrome", "google chrome"}
            else f"https://duckduckgo.com/?q={encoded}"
        )
        opened_in = "embedded_viewer"
        if force_external or os.environ.get("JARVIS_EMBEDDED_VIEWER") != "1":
            opened_in = _open_url_external(search_url, browser=browser, same_tab=bool(same_tab))
        weather_snippets = await _fetch_live_weather_snippet(cleaned)
        snippets = weather_snippets if weather_snippets else await _fetch_web_snippets(cleaned, max_results=3)
        res: dict[str, Any] = {
            "status": "ok",
            "query": cleaned,
            "kind": "web_search",
            "url": search_url,
            "opened_in": opened_in,
            "title": f"Search: {cleaned}",
            "snippets": snippets,
        }
        if not force_external:
            res["viewer_url"] = search_url
        if not snippets:
            res["hint"] = (
                f"Search opened in {opened_in}. "
                "Do NOT repeat web_search in a loop — answer concisely or inform the user."
            )
        return res

    def enter_standby(reason: str = "") -> dict[str, Any]:
        return {
            "status": "ok",
            "standby": True,
            "reason": reason.strip(),
            "message": "Returning to idle wake-word standby.",
        }

    def _send_win_vk(vk_code: int, taps: int = 1) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows virtual key events are only supported on Windows.")
        user32 = ctypes.windll.user32
        keyeventf_keyup = 0x0002
        for _ in range(max(1, taps)):
            user32.keybd_event(vk_code, 0, 0, 0)
            user32.keybd_event(vk_code, 0, keyeventf_keyup, 0)

    def system_control(
        action: str,
        value: int = 0,
        text: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Control Windows audio volume, media playback, screen brightness, clipboard, or workstation lock."""
        act = action.strip().lower()
        vk_map = {
            "volume_up": 0xAF,
            "volume_down": 0xAE,
            "mute_toggle": 0xAD,
            "media_play_pause": 0xB3,
            "media_next": 0xB0,
            "media_prev": 0xB1,
        }
        if act in vk_map:
            taps = max(1, min(20, int(value or (5 if act in {"volume_up", "volume_down"} else 1))))
            _send_win_vk(vk_map[act], taps=taps)
            return {"status": "ok", "action": act, "taps": taps}

        if act == "clipboard_get":
            if os.name != "nt":
                return {"status": "error", "error": "Clipboard access is only supported on Windows."}
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode != 0:
                return {"status": "error", "error": (proc.stderr or "Failed to read clipboard").strip()}
            clip = proc.stdout or ""
            return {
                "status": "ok",
                "action": act,
                "text": _truncate(clip, 3000),
                "length": len(clip),
            }

        if act == "clipboard_set":
            if os.name != "nt":
                return {"status": "error", "error": "Clipboard access is only supported on Windows."}
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "$inText = [Console]::In.ReadToEnd(); Set-Clipboard -Value $inText"],
                input=text,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode != 0:
                return {"status": "error", "error": (proc.stderr or "Failed to write clipboard").strip()}
            return {"status": "ok", "action": act, "bytes": len(text.encode("utf-8"))}

        if act in {"brightness_set", "brightness_up", "brightness_down"}:
            if os.name != "nt":
                return {"status": "error", "error": "Brightness control is only supported on Windows."}
            if act == "brightness_set":
                level = max(0, min(100, int(value)))
                ps_cmd = (
                    "$m = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | Select-Object -First 1; "
                    f"$null = Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={level}}}; "
                    f"Write-Output {level}"
                )
            else:
                delta = 15 if act == "brightness_up" else -15
                ps_cmd = (
                    "$cur = (Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness -ErrorAction Stop | Select-Object -First 1).CurrentBrightness; "
                    f"$target = [Math]::Max(0, [Math]::Min(100, [int]$cur + ({delta}))); "
                    "$m = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | Select-Object -First 1; "
                    "$null = Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness -Arguments @{Timeout=1; Brightness=$target}; "
                    "Write-Output $target"
                )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                return {
                    "status": "error",
                    "error": (proc.stderr or "Display brightness WMI interface is unavailable on this monitor.").strip()[:400],
                }
            return {"status": "ok", "action": act, "brightness": proc.stdout.strip()}

        if act == "lock_screen":
            if not (confirmed or config.auto_approve_dangerous):
                return {
                    "status": "needs_confirmation",
                    "action": act,
                    "message": "Locking the Windows workstation requires user confirmation. Re-invoke with confirmed=true if requested.",
                }
            if os.name != "nt":
                return {"status": "error", "error": "Workstation lock is only supported on Windows."}
            ctypes.windll.user32.LockWorkStation()
            return {"status": "ok", "action": act}

        return {"status": "error", "error": f"Unsupported system_control action: {action}"}

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
            description="Open a Windows application (e.g. notepad, calculator, vscode, chrome, terminal), file, or HTTPS URL (in Jarvis's embedded viewer by default, or in Google Chrome/external browser when requested).",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "target": {
                        "type": "STRING",
                        "description": "App name, executable, local path, or https:// URL.",
                    },
                    "browser": {
                        "type": "STRING",
                        "enum": ["viewer", "chrome", "external"],
                        "description": "Where to open HTTPS URLs: 'viewer' (Jarvis's built-in HUD browser, default), 'chrome' (Google Chrome), or 'external' (OS default browser).",
                    },
                    "same_tab": {
                        "type": "BOOLEAN",
                        "description": "When browser='chrome', set true to reuse the active Chrome tab instead of opening a new tab (e.g. 'en esta misma pestaña' / 'same tab').",
                    },
                },
                "required": ["target"],
            },
            handler=open_application_or_url,
        )
    )
    registry.register(
        ToolSpec(
            name="web_search",
            description="Search the web for a query, returning up to 3 concise text snippets and opening live results in Jarvis's built-in viewer or Google Chrome/external browser.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "Search query to look up on the web.",
                    },
                    "browser": {
                        "type": "STRING",
                        "enum": ["viewer", "chrome", "external"],
                        "description": "Where to open results: 'viewer' (Jarvis's built-in HUD browser, default), 'chrome' (Google Chrome), or 'external' (OS default browser).",
                    },
                    "same_tab": {
                        "type": "BOOLEAN",
                        "description": "When browser='chrome', set true to search inside the active Chrome tab instead of opening a new tab.",
                    },
                },
                "required": ["query"],
            },
            handler=web_search,
        )
    )
    registry.register(
        ToolSpec(
            name="enter_standby",
            description=(
                "ONLY call when the user explicitly commands Jarvis to go to sleep, enter standby, "
                "stop listening, or says goodbye (e.g. 'standby', 'go to sleep', 'descansa', 'adiós', "
                "'eso es todo'). NEVER call during unfinished sentences, pauses, or unrelated requests."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "reason": {
                        "type": "STRING",
                        "description": "Exact short user phrase requesting standby/goodbye.",
                    }
                },
            },
            handler=enter_standby,
        )
    )
    registry.register(
        ToolSpec(
            name="system_control",
            description=(
                "Control Windows OS hardware & media quickly: volume_up, volume_down, mute_toggle, "
                "media_play_pause, media_next, media_prev, brightness_set, brightness_up, brightness_down, "
                "clipboard_get, clipboard_set, or lock_screen."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "action": {
                        "type": "STRING",
                        "enum": [
                            "volume_up",
                            "volume_down",
                            "mute_toggle",
                            "media_play_pause",
                            "media_next",
                            "media_prev",
                            "brightness_set",
                            "brightness_up",
                            "brightness_down",
                            "clipboard_get",
                            "clipboard_set",
                            "lock_screen",
                        ],
                    },
                    "value": {
                        "type": "INTEGER",
                        "description": "Step count (1-20) for volume_up/down or target percentage (0-100) for brightness_set.",
                    },
                    "text": {
                        "type": "STRING",
                        "description": "Text to copy into the Windows clipboard (used with clipboard_set).",
                    },
                    "confirmed": {
                        "type": "BOOLEAN",
                        "description": "Set true when confirming lock_screen.",
                    },
                },
                "required": ["action"],
            },
            handler=system_control,
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
            description="Create or update a text/code file at any accessible path.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Relative or absolute file path."},
                    "content": {"type": "STRING", "description": "Full text content to write."},
                    "overwrite": {"type": "BOOLEAN", "description": "Set true to overwrite an existing file."},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        )
    )
    registry.register(
        ToolSpec(
            name="edit_file",
            description="Replace a specific text block inside an existing file.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Relative or absolute file path."},
                    "target_text": {"type": "STRING", "description": "Exact existing text to replace."},
                    "replacement": {"type": "STRING", "description": "New text to substitute."},
                },
                "required": ["path", "target_text", "replacement"],
            },
            handler=edit_file,
        )
    )
    registry.register(
        ToolSpec(
            name="create_document",
            description="Create a formatted document (.pdf, .docx, .md, .txt, .html) supporting Markdown headings, tables, lists, bold/italic, and academic styles (standard, apa, ieee, report).",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Output file path ending in .pdf, .docx, .md, .txt, or .html."},
                    "title": {"type": "STRING", "description": "Document title."},
                    "content": {"type": "STRING", "description": "Full Markdown or plain body text (supports # headings, **bold**, *italic*, bullet/numbered lists, and | pipe tables |)."},
                    "style": {"type": "STRING", "description": "Formatting style: 'standard', 'apa' (Times New Roman 12pt, double-spaced, 1-in margins, cover & page numbers), 'ieee', or 'report'."},
                    "subtitle": {"type": "STRING", "description": "Optional subtitle."},
                    "author": {"type": "STRING", "description": "Optional author name for cover header."},
                    "institution": {"type": "STRING", "description": "Optional university/organization name."},
                    "course": {"type": "STRING", "description": "Optional course or department name."},
                    "date": {"type": "STRING", "description": "Optional date string."},
                    "references": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Optional list of bibliography/reference entries.",
                    },
                    "overwrite": {"type": "BOOLEAN", "description": "Set true to overwrite an existing file."},
                },
                "required": ["path", "content"],
            },
            handler=create_document,
        )
    )
