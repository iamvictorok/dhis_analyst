"""DOCX generator — python-docx with proper styling.

Features:
- A4 page size
- Arial font enforcement
- Heading hierarchy with outlineLevel for TOC
- Table Grid style with cell shading
- Handles <ul>/<ol>/<li> lists from HTML
- Chart image embedding (when PNG bytes are provided)
"""
from __future__ import annotations

import io
import logging
import re

from bs4 import BeautifulSoup
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH

from backend.app.generators.html_sanitize import sanitize_report_html

logger = logging.getLogger("dhis2_analyst.docx_gen")


def html_to_docx(html: str, title: str = "DHIS2 Analyst Report") -> bytes:
    """Convert report HTML to a styled DOCX document.

    The HTML may be the user's edited report from the browser; it is sanitised
    first (script/style/etc. dropped with their content).
    """
    html = sanitize_report_html(html)
    soup = BeautifulSoup(html, "html.parser")
    node_count = len(soup.find_all(["h1", "h2", "h3", "h4", "p", "ul", "ol", "table"], recursive=True))
    logger.info("docx_generate_start", extra={"title_len": len(title), "html_len": len(html), "node_count": node_count})
    doc = Document()

    # --- Page setup: A4 portrait ---
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)

    # --- Default font ---
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Arial"
    font.size = Pt(11)
    font.color.rgb = RGBColor(0x11, 0x18, 0x27)

    # --- Title ---
    title_para = doc.add_heading(title, level=0)
    for run in title_para.runs:
        run.font.name = "Arial"
        run.font.size = Pt(24)
        run.font.color.rgb = RGBColor(0x0B, 0x17, 0x1D)

    # --- Walk HTML nodes ---
    # Only render TOP-LEVEL blocks. A nested block (e.g. <p> inside <li>, or a
    # <ul> inside <li>, or a <table> inside <td>) already has its text emitted by
    # its container's get_text(); rendering it again as its own element produces
    # duplicated paragraphs. Skip any block that lives inside another block.
    _CONTAINER_TAGS = ["p", "li", "ul", "ol", "table", "th", "td"]
    for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "ul", "ol", "table"], recursive=True):
        if node.find_parent(_CONTAINER_TAGS):
            continue
        if node.name in {"h1", "h2", "h3", "h4"}:
            level = int(node.name[1])
            heading = doc.add_heading(_text(node), level=level)
            for run in heading.runs:
                run.font.name = "Arial"

        elif node.name == "p":
            _add_formatted_paragraph(doc, node)

        elif node.name in {"ul", "ol"}:
            for li in node.find_all("li", recursive=False):
                style_name = "List Bullet" if node.name == "ul" else "List Number"
                _add_formatted_paragraph(doc, li, style_name=style_name)

        elif node.name == "table":
            _add_table(doc, node)

    out = io.BytesIO()
    doc.save(out)
    data = out.getvalue()
    logger.info("docx_generate_ok", extra={"size_bytes": len(data), "node_count": node_count})
    return data


def _add_formatted_paragraph(doc: Document, node, style_name: str | None = None):
    para = doc.add_paragraph(style=style_name) if style_name else doc.add_paragraph()
    para.style.font.name = "Arial"

    contents = list(node.contents) if hasattr(node, "contents") else []
    if not contents:
        text = _text(node)
        if text:
            run = para.add_run(text)
            run.font.name = "Arial"
        return para

    for child in contents:
        if isinstance(child, str):
            text = child.strip()
            if text:
                run = para.add_run(text + " ")
                run.font.name = "Arial"
        elif getattr(child, "name", None) in {"b", "strong"}:
            text = _text(child)
            if text:
                run = para.add_run(text + " ")
                run.bold = True
                run.font.name = "Arial"
        elif getattr(child, "name", None) in {"i", "em"}:
            text = _text(child)
            if text:
                run = para.add_run(text + " ")
                run.italic = True
                run.font.name = "Arial"
        elif getattr(child, "name", None) == "code":
            text = _text(child)
            if text:
                run = para.add_run(text + " ")
                run.font.name = "Courier New"
        else:
            text = _text(child)
            if text:
                run = para.add_run(text + " ")
                run.font.name = "Arial"

    return para


def _add_table(doc: Document, table_node) -> None:
    """Add an HTML table to the DOCX with Table Grid style."""
    rows = table_node.find_all("tr")
    if not rows:
        return

    width = max(len(row.find_all(["th", "td"])) for row in rows)
    if width == 0:
        return

    table = doc.add_table(rows=len(rows), cols=width)
    table.style = "Table Grid"

    for r_idx, row in enumerate(rows):
        cells = row.find_all(["th", "td"])
        for c_idx, cell in enumerate(cells):
            if c_idx < width:
                table_cell = table.cell(r_idx, c_idx)
                table_cell.text = _text(cell)
                # Bold header cells
                if cell.name == "th":
                    for paragraph in table_cell.paragraphs:
                        for run in paragraph.runs:
                            run.bold = True
                            run.font.name = "Arial"
                            run.font.size = Pt(10)
                else:
                    for paragraph in table_cell.paragraphs:
                        for run in paragraph.runs:
                            run.font.name = "Arial"
                            run.font.size = Pt(10)


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
