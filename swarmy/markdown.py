"""Markdown to HTML with markdown-it-py (CommonMark + tables + strikethrough).

Raw HTML in the source is escaped (html=False) and unsafe link schemes such as javascript: are
refused by markdown-it's link validation, so the output is safe to insert into the designer's pages.
"""

from __future__ import annotations

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])


def to_html(text: str | None) -> str:
    return _md.render(text or "")
