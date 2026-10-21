"""Allow-list sanitiser for report HTML posted to the DOCX/PDF generators.

The report export endpoints accept HTML from the browser: the generated report,
or the user's edited copy of it from the report editor. That HTML is
user-controlled, so before it reaches WeasyPrint or python-docx we:

- drop dangerous elements together with their content (script, style, iframe,
  object, embed, form, svg, math, img, link, meta, base, ...). WeasyPrint does not
  run scripts, but it does fetch ``<img src>``, ``<link href>`` and CSS ``url()``
  targets, which would let a crafted report read local files or reach internal
  hosts (SSRF);
- unwrap any other unknown tag (its text is kept, the tag is not);
- keep only safe attributes; ``href`` only for http(s)/mailto/#anchors; ``style``
  only when it has no ``url()``, ``expression()``, ``@import``, ``javascript:``
  or backslash escapes;
- drop comments, processing instructions and doctype nodes.

If nothing needs changing, the ORIGINAL string is returned untouched, so a
report the user did not edit is rendered exactly as before this sanitiser
existed (byte-for-byte the same input to the generators).
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction
from bs4.element import CData

# Formatting a report can legitimately use (Tiptap StarterKit + tables + what
# the report renderer / LLM emit).
ALLOWED_TAGS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr", "div", "span", "blockquote", "pre", "code",
    "strong", "b", "em", "i", "u", "s", "strike", "del", "ins", "mark",
    "sub", "sup", "small",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr", "th", "td",
    "a",
    "section", "article", "header", "footer", "figure", "figcaption",
})

# Removed WITH their content.
DROP_TAGS = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet",
    "form", "input", "button", "select", "textarea", "option",
    "svg", "math", "img", "picture", "source", "video", "audio", "track", "canvas",
    "link", "meta", "base", "noscript", "template", "head", "title",
    "portal", "param",
})

ALLOWED_ATTRS = frozenset({
    "class", "id", "title", "lang", "dir",
    "colspan", "rowspan", "scope", "headers", "span",
    "align", "valign", "width", "height", "border", "cellpadding", "cellspacing",
    "start", "type", "reversed", "value",
    "href", "style",
    # Tiptap emits these on tables / cells; harmless.
    "colwidth", "data-colwidth",
})

_SAFE_HREF = re.compile(r"^\s*(https?:|mailto:|#)", re.I)
_BAD_STYLE = re.compile(r"url\s*\(|expression\s*\(|@import|javascript:|behavio(u)?r\s*:|-moz-binding|\\", re.I)


def sanitize_report_html(html: str) -> str:
    """Return HTML safe to hand to WeasyPrint / python-docx.

    Returns the input unchanged when it contains nothing to remove.
    """
    if not isinstance(html, str) or not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    changed = False

    # Comments, doctype, CDATA, processing instructions: never needed.
    for node in list(soup.find_all(string=True)):
        if isinstance(node, (Comment, Doctype, Declaration, ProcessingInstruction, CData)):
            node.extract()
            changed = True

    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False):
            # Already removed along with a dropped ancestor.
            continue
        name = (tag.name or "").lower()
        if name in DROP_TAGS:
            tag.decompose()
            changed = True
            continue
        if name not in ALLOWED_TAGS:
            tag.unwrap()
            changed = True
            continue
        for attr in list(tag.attrs):
            key = attr.lower()
            raw = tag.attrs[attr]
            value = " ".join(raw) if isinstance(raw, list) else str(raw)
            keep = key in ALLOWED_ATTRS
            if keep and key == "href":
                keep = name == "a" and bool(_SAFE_HREF.match(value))
            elif keep and key == "style":
                keep = not _BAD_STYLE.search(value)
            if not keep:
                del tag.attrs[attr]
                changed = True

    if not changed:
        return html
    return str(soup)


def report_text(html: str) -> str:
    """Plain text of report HTML (for tests and logging)."""
    soup = BeautifulSoup(html or "", "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


__all__ = ["sanitize_report_html", "report_text", "ALLOWED_TAGS", "DROP_TAGS"]
