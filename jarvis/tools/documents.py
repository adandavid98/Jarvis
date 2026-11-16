"""Structured & academic document generator (.docx, .pdf, .md, .txt) for Jarvis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_INLINE_TOKEN_RE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")
_NUMBERED_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")


@dataclass(slots=True)
class DocumentMeta:
    """Optional academic or report metadata for structured document rendering."""

    title: str = ""
    subtitle: str = ""
    author: str = ""
    institution: str = ""
    course: str = ""
    date: str = ""
    style: str = "standard"  # standard | apa | ieee | report
    references: list[str] | None = None


def _parse_markdown_blocks(content: str) -> list[dict[str, Any]]:
    """Parse lightweight Markdown into typed document blocks."""
    blocks: list[dict[str, Any]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append({"type": "code", "text": "\n".join(code_lines)})
            continue

        # Horizontal rule
        if stripped in {"---", "***", "___"}:
            blocks.append({"type": "hr"})
            i += 1
            continue

        # Headings
        if stripped.startswith("### "):
            blocks.append({"type": "heading", "level": 3, "text": stripped[4:].strip()})
            i += 1
            continue
        if stripped.startswith("## "):
            blocks.append({"type": "heading", "level": 2, "text": stripped[3:].strip()})
            i += 1
            continue
        if stripped.startswith("# "):
            blocks.append({"type": "heading", "level": 1, "text": stripped[2:].strip()})
            i += 1
            continue

        # Pipe tables
        if stripped.startswith("|") and "|" in stripped[1:]:
            table_rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row_txt = lines[i].strip().strip("|")
                cells = [c.strip() for c in row_txt.split("|")]
                # Skip Markdown alignment separator rows (e.g. --- | :---:)
                if cells and not all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells):
                    table_rows.append(cells)
                i += 1
            if table_rows:
                blocks.append({"type": "table", "rows": table_rows})
            continue

        # Bullet list items
        if stripped.startswith(("- ", "* ", "• ")):
            blocks.append({"type": "bullet", "text": stripped[2:].strip()})
            i += 1
            continue

        # Numbered list items
        num_m = _NUMBERED_RE.match(stripped)
        if num_m:
            blocks.append({"type": "numbered", "index": num_m.group(1), "text": num_m.group(2).strip()})
            i += 1
            continue

        # Coalesce consecutive plain lines into one paragraph
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith(("# ", "## ", "### ", "- ", "* ", "• ", "|", "```"))
                or nxt in {"---", "***", "___"}
                or _NUMBERED_RE.match(nxt)
            ):
                break
            para_lines.append(nxt)
            i += 1
        blocks.append({"type": "paragraph", "text": " ".join(para_lines)})

    return blocks


def _split_inline_runs(text: str) -> list[tuple[str, str]]:
    """Split text into `(style, text)` spans where style is plain/bold/italic/code."""
    spans: list[tuple[str, str]] = []
    pos = 0
    for match in _INLINE_TOKEN_RE.finditer(text):
        if match.start() > pos:
            spans.append(("plain", text[pos : match.start()]))
        token = match.group(0)
        if token.startswith("**") and token.endswith("**"):
            spans.append(("bold", token[2:-2]))
        elif token.startswith("*") and token.endswith("*"):
            spans.append(("italic", token[1:-1]))
        elif token.startswith("`") and token.endswith("`"):
            spans.append(("code", token[1:-1]))
        else:
            spans.append(("plain", token))
        pos = match.end()
    if pos < len(text):
        spans.append(("plain", text[pos:]))
    return spans or [("plain", text)]


def _add_docx_inline_runs(paragraph: Any, text: str, font_name: str, font_pt: float) -> None:
    from docx.shared import Pt

    for kind, span in _split_inline_runs(text):
        run = paragraph.add_run(span)
        run.font.name = "Consolas" if kind == "code" else font_name
        run.font.size = Pt(max(9.0, font_pt - 1) if kind == "code" else font_pt)
        if kind == "bold":
            run.bold = True
        elif kind == "italic":
            run.italic = True


def _add_docx_page_number(section: Any, font_name: str) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    header = section.header
    p = header.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run()
    run.font.name = font_name
    run.font.size = Pt(11)
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


def _render_docx(target: Path, content: str, meta: DocumentMeta) -> None:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    style_key = (meta.style or "standard").strip().lower()
    is_apa = style_key == "apa"
    is_ieee = style_key == "ieee"
    font_name = "Times New Roman" if (is_apa or is_ieee) else "Calibri"
    body_pt = 12.0 if is_apa else (10.0 if is_ieee else 11.0)
    line_spacing = 2.0 if is_apa else (1.15 if style_key == "report" else 1.25)

    doc = Document()
    for section in doc.sections:
        margin = Inches(1.0) if is_apa else Inches(0.85)
        section.top_margin = margin
        section.bottom_margin = margin
        section.left_margin = margin
        section.right_margin = margin
        if is_apa or style_key == "report":
            _add_docx_page_number(section, font_name)

    normal_style = doc.styles["Normal"]
    normal_style.font.name = font_name
    normal_style.font.size = Pt(body_pt)

    # Academic / Structured Cover or Title Header
    if meta.title:
        title_p = doc.add_paragraph()
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER if (is_apa or is_ieee) else WD_ALIGN_PARAGRAPH.LEFT
        title_p.paragraph_format.space_before = Pt(36 if is_apa else 0)
        title_p.paragraph_format.space_after = Pt(6)
        title_run = title_p.add_run(meta.title)
        title_run.bold = True
        title_run.font.name = font_name
        title_run.font.size = Pt(16 if not is_apa else 14)
        if style_key == "report":
            title_run.font.color.rgb = RGBColor(0x0B, 0x4F, 0x6C)

    if meta.subtitle:
        sub_p = doc.add_paragraph()
        sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER if (is_apa or is_ieee) else WD_ALIGN_PARAGRAPH.LEFT
        sub_p.paragraph_format.space_after = Pt(8)
        sub_r = sub_p.add_run(meta.subtitle)
        sub_r.italic = True
        sub_r.font.name = font_name
        sub_r.font.size = Pt(body_pt)

    meta_lines = [m for m in (meta.author, meta.institution, meta.course, meta.date) if m and m.strip()]
    if meta_lines:
        for line in meta_lines:
            mp = doc.add_paragraph()
            mp.alignment = WD_ALIGN_PARAGRAPH.CENTER if (is_apa or is_ieee) else WD_ALIGN_PARAGRAPH.LEFT
            mp.paragraph_format.line_spacing = line_spacing
            mp.paragraph_format.space_after = Pt(2)
            mr = mp.add_run(line.strip())
            mr.font.name = font_name
            mr.font.size = Pt(body_pt)
        if is_apa:
            doc.add_page_break()
        else:
            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(6)

    for block in _parse_markdown_blocks(content):
        btype = block["type"]
        if btype == "heading":
            lvl = int(block["level"])
            hp = doc.add_paragraph()
            hp.paragraph_format.keep_with_next = True
            hp.paragraph_format.line_spacing = line_spacing
            hp.paragraph_format.space_before = Pt(10)
            hp.paragraph_format.space_after = Pt(4)
            if is_apa and lvl == 1:
                hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            hr = hp.add_run(block["text"])
            hr.bold = True
            hr.italic = is_apa and lvl == 3
            hr.font.name = font_name
            hr.font.size = Pt(body_pt + (3 if lvl == 1 else 1 if lvl == 2 else 0))
            if style_key == "report" and lvl <= 2:
                hr.font.color.rgb = RGBColor(0x0B, 0x4F, 0x6C)
        elif btype == "paragraph":
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing = line_spacing
            p.paragraph_format.space_after = Pt(0 if is_apa else 6)
            if is_apa:
                p.paragraph_format.first_line_indent = Inches(0.5)
            _add_docx_inline_runs(p, block["text"], font_name, body_pt)
        elif btype == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.line_spacing = line_spacing
            _add_docx_inline_runs(p, block["text"], font_name, body_pt)
        elif btype == "numbered":
            p = doc.add_paragraph(style="List Number")
            p.paragraph_format.line_spacing = line_spacing
            _add_docx_inline_runs(p, block["text"], font_name, body_pt)
        elif btype == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.35)
            p.paragraph_format.space_after = Pt(6)
            run = p.add_run(block["text"])
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)
        elif btype == "table":
            rows = block["rows"]
            cols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=cols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for r_idx, row_vals in enumerate(rows):
                for c_idx in range(cols):
                    cell = table.cell(r_idx, c_idx)
                    cell_txt = row_vals[c_idx] if c_idx < len(row_vals) else ""
                    cp = cell.paragraphs[0]
                    cp.paragraph_format.space_after = Pt(2)
                    cr = cp.add_run(cell_txt)
                    cr.font.name = font_name
                    cr.font.size = Pt(max(9.5, body_pt - 1))
                    if r_idx == 0:
                        cr.bold = True
        elif btype == "hr":
            p = doc.add_paragraph("─" * 48)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if meta.references:
        if is_apa:
            doc.add_page_break()
        ref_hdr = doc.add_paragraph()
        ref_hdr.alignment = WD_ALIGN_PARAGRAPH.CENTER if is_apa else WD_ALIGN_PARAGRAPH.LEFT
        ref_hdr.paragraph_format.space_before = Pt(10)
        ref_hdr.paragraph_format.space_after = Pt(6)
        ref_r = ref_hdr.add_run("References" if is_apa else "Referencias")
        ref_r.bold = True
        ref_r.font.name = font_name
        ref_r.font.size = Pt(body_pt + (2 if not is_apa else 0))
        for ref in meta.references:
            if not ref.strip():
                continue
            rp = doc.add_paragraph()
            rp.paragraph_format.line_spacing = line_spacing
            rp.paragraph_format.left_indent = Inches(0.5)
            rp.paragraph_format.first_line_indent = Inches(-0.5)
            rp.paragraph_format.space_after = Pt(4)
            rr = rp.add_run(ref.strip())
            rr.font.name = font_name
            rr.font.size = Pt(body_pt)

    doc.save(str(target))


def _strip_inline_markers(text: str) -> str:
    return "".join(span for _, span in _split_inline_runs(text))


def _render_pdf(target: Path, content: str, meta: DocumentMeta) -> None:
    from fpdf import FPDF

    style_key = (meta.style or "standard").strip().lower()
    is_apa = style_key == "apa"

    class _AcademicPDF(FPDF):
        def header(self_pdf) -> None:
            if self_pdf.page_no() == 1 and not is_apa:
                return
            self_pdf.set_font(font_family, "", 9)
            self_pdf.set_text_color(100, 110, 120)
            label = meta.title[:45] if is_apa and meta.title else ""
            if label:
                self_pdf.cell(0, 6, label, align="L")
            self_pdf.cell(0, 6, str(self_pdf.page_no()), align="R", new_x="LMARGIN", new_y="NEXT")
            self_pdf.set_text_color(20, 20, 20)

    pdf = _AcademicPDF()
    margin = 25.4 if is_apa else 18.0
    pdf.set_margins(margin, margin, margin)
    pdf.set_auto_page_break(auto=True, margin=margin)

    font_family = "Helvetica"
    unicode_font = Path(r"C:\Windows\Fonts\arial.ttf")
    bold_font = Path(r"C:\Windows\Fonts\arialbd.ttf")
    italic_font = Path(r"C:\Windows\Fonts\ariali.ttf")
    has_bold = True
    has_italic = True
    if unicode_font.exists():
        pdf.add_font("ArialUnicode", "", str(unicode_font))
        has_bold = bold_font.exists()
        has_italic = italic_font.exists()
        if has_bold:
            pdf.add_font("ArialUnicode", "B", str(bold_font))
        if has_italic:
            pdf.add_font("ArialUnicode", "I", str(italic_font))
        font_family = "ArialUnicode"

    pdf.add_page()
    line_h = 8.0 if is_apa else 6.0

    if meta.title:
        pdf.set_font(font_family, "B" if has_bold else "", 16)
        pdf.multi_cell(0, 8.5, meta.title, align="C" if is_apa else "L")
        pdf.ln(2)
    if meta.subtitle:
        pdf.set_font(font_family, "I" if has_italic else "", 12)
        pdf.multi_cell(0, 6.5, meta.subtitle, align="C" if is_apa else "L")
        pdf.ln(2)

    meta_lines = [m for m in (meta.author, meta.institution, meta.course, meta.date) if m and m.strip()]
    if meta_lines:
        pdf.set_font(font_family, "", 11)
        for line in meta_lines:
            pdf.multi_cell(0, 6.0, line.strip(), align="C" if is_apa else "L")
        if is_apa:
            pdf.add_page()
        else:
            pdf.ln(4)

    for block in _parse_markdown_blocks(content):
        btype = block["type"]
        if btype == "heading":
            lvl = int(block["level"])
            pdf.ln(2)
            pdf.set_font(font_family, "B" if has_bold else "", 14 if lvl == 1 else 12 if lvl == 2 else 11)
            pdf.multi_cell(0, 7.0, block["text"], align="C" if (is_apa and lvl == 1) else "L")
            pdf.ln(1.5)
        elif btype == "paragraph":
            pdf.set_font(font_family, "", 11)
            prefix = "    " if is_apa else ""
            pdf.multi_cell(0, line_h, prefix + _strip_inline_markers(block["text"]))
            pdf.ln(1.5 if is_apa else 2.5)
        elif btype == "bullet":
            pdf.set_font(font_family, "", 11)
            pdf.multi_cell(0, line_h, f"  • {_strip_inline_markers(block['text'])}")
            pdf.ln(1.0)
        elif btype == "numbered":
            pdf.set_font(font_family, "", 11)
            pdf.multi_cell(0, line_h, f"  {block['index']}. {_strip_inline_markers(block['text'])}")
            pdf.ln(1.0)
        elif btype == "code":
            pdf.set_fill_color(242, 245, 248)
            pdf.set_font("Courier", "", 9.5)
            pdf.multi_cell(0, 5.2, block["text"], fill=True)
            pdf.ln(2)
        elif btype == "table":
            rows = block["rows"]
            cols = max(len(r) for r in rows)
            usable_w = pdf.w - 2 * margin
            col_w = usable_w / max(1, cols)
            for r_idx, row_vals in enumerate(rows):
                pdf.set_font(font_family, "B" if (r_idx == 0 and has_bold) else "", 10)
                if r_idx == 0:
                    pdf.set_fill_color(230, 242, 245)
                for c_idx in range(cols):
                    val = row_vals[c_idx] if c_idx < len(row_vals) else ""
                    pdf.cell(col_w, 7.0, val[:42], border=1, fill=(r_idx == 0))
                pdf.ln()
            pdf.ln(2)
        elif btype == "hr":
            pdf.ln(2)
            y = pdf.get_y()
            pdf.line(margin, y, pdf.w - margin, y)
            pdf.ln(3)

    if meta.references:
        if is_apa:
            pdf.add_page()
        pdf.set_font(font_family, "B" if has_bold else "", 13)
        pdf.multi_cell(0, 7.0, "References" if is_apa else "Referencias", align="C" if is_apa else "L")
        pdf.ln(2)
        pdf.set_font(font_family, "", 10.5)
        for ref in meta.references:
            if ref.strip():
                pdf.multi_cell(0, line_h, ref.strip())
                pdf.ln(1.5)

    pdf.output(str(target))


def generate_document_file(
    target: Path,
    *,
    content: str,
    title: str = "",
    subtitle: str = "",
    author: str = "",
    institution: str = "",
    course: str = "",
    date: str = "",
    style: str = "standard",
    references: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a `.pdf`, `.docx`, `.md`, or `.txt` file on disk."""
    target.parent.mkdir(parents=True, exist_ok=True)
    ext = target.suffix.lower()
    meta = DocumentMeta(
        title=title.strip(),
        subtitle=subtitle.strip(),
        author=author.strip(),
        institution=institution.strip(),
        course=course.strip(),
        date=date.strip(),
        style=(style or "standard").strip().lower(),
        references=references or None,
    )

    if ext == ".pdf":
        _render_pdf(target, content, meta)
    elif ext == ".docx":
        _render_docx(target, content, meta)
    else:
        header_parts: list[str] = []
        if meta.title:
            header_parts.append(f"# {meta.title}" if ext == ".md" else meta.title)
        if meta.subtitle:
            header_parts.append(f"*{meta.subtitle}*" if ext == ".md" else meta.subtitle)
        for m in (meta.author, meta.institution, meta.course, meta.date):
            if m:
                header_parts.append(m)
        parts = ["\n\n".join(header_parts), content] if header_parts else [content]
        if meta.references:
            refs_md = "\n".join(f"- {r}" for r in meta.references if r.strip())
            parts.append(f"## References\n\n{refs_md}")
        target.write_text("\n\n".join(p for p in parts if p).strip() + "\n", encoding="utf-8")

    return {
        "status": "ok",
        "path": str(target),
        "format": ext.lstrip(".") or "txt",
        "style": meta.style,
        "bytes_written": target.stat().st_size,
    }
