"""Deep web page and local PDF reading tools for Jarvis (with token-safe caps)."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import httpx

from jarvis.config import JarvisConfig
from jarvis.tools._paths import resolve_workspace_path
from jarvis.tools.registry import ToolRegistry, ToolSpec

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def register_research_tools(registry: ToolRegistry, config: JarvisConfig) -> None:
    """Register deep web page and local PDF reading tools."""

    async def fetch_web_page(url: str, max_chars: int = 8000) -> dict[str, Any]:
        cleaned = url.strip()
        if not cleaned.lower().startswith(("http://", "https://")):
            return {"status": "error", "error": "URL must start with http:// or https://"}

        cap = max(500, min(15000, int(max_chars)))
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
        }
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(cleaned)
            resp.raise_for_status()
            raw_html = resp.text

        title_match = _TITLE_RE.search(raw_html)
        page_title = " ".join(html.unescape(title_match.group(1)).split())[:120] if title_match else cleaned
        stripped = _SCRIPT_STYLE_RE.sub(" ", raw_html)
        plain = " ".join(html.unescape(_TAG_RE.sub(" ", stripped)).split())
        truncated = len(plain) > cap
        if truncated:
            plain = plain[:cap].rstrip() + "\n...[truncated for token safety]"

        final_url = str(resp.url)
        return {
            "status": "ok",
            "url": final_url,
            "title": page_title,
            "content": plain,
            "truncated": truncated,
            "viewer_url": final_url,
        }

    def read_pdf(path: str, start_page: int = 1, max_pages: int = 8, max_chars: int = 10000) -> dict[str, Any]:
        from pypdf import PdfReader

        target = resolve_workspace_path(
            config.workspace_dir,
            path,
            allow_outside=True,
        )
        if not target.exists() or not target.is_file():
            return {"status": "error", "error": f"PDF not found: {target}"}

        reader = PdfReader(str(target))
        total_pages = len(reader.pages)
        start_idx = max(0, int(start_page) - 1)
        page_cap = max(1, min(20, int(max_pages)))
        char_cap = max(500, min(20000, int(max_chars)))
        end_idx = min(total_pages, start_idx + page_cap)

        chunks: list[str] = []
        used = 0
        for idx in range(start_idx, end_idx):
            page_txt = (reader.pages[idx].extract_text() or "").strip()
            block = f"[Page {idx + 1}/{total_pages}]\n{page_txt}"
            if used + len(block) > char_cap:
                remain = max(0, char_cap - used)
                if remain > 60:
                    chunks.append(block[:remain].rstrip() + "\n...[truncated]")
                break
            chunks.append(block)
            used += len(block) + 2

        return {
            "status": "ok",
            "path": str(target),
            "total_pages": total_pages,
            "start_page": start_idx + 1,
            "pages_read": len(chunks),
            "content": "\n\n".join(chunks),
        }

    registry.register(
        ToolSpec(
            name="fetch_web_page",
            description="Fetch and extract clean readable text from an HTTP/HTTPS web page for research or documentation reading.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "url": {"type": "STRING", "description": "Full https:// URL to read."},
                    "max_chars": {"type": "INTEGER", "description": "Optional character cap (default 8000, max 15000)."},
                },
                "required": ["url"],
            },
            handler=fetch_web_page,
        )
    )
    registry.register(
        ToolSpec(
            name="read_pdf",
            description="Extract readable text from a local PDF file (e.g. academic papers, manuals, assignments).",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Path to the .pdf file."},
                    "start_page": {"type": "INTEGER", "description": "1-based page number to start reading (default 1)."},
                    "max_pages": {"type": "INTEGER", "description": "Maximum pages to extract (default 8, max 20)."},
                    "max_chars": {"type": "INTEGER", "description": "Character limit to keep token usage safe (default 10000)."},
                },
                "required": ["path"],
            },
            handler=read_pdf,
        )
    )
