"""ATX header parsing: listing a document's headers and slicing out one section.

Slugs mirror the frontend's ``slugifyHeader`` (GitHub-style), so a slug taken from a header share
link addresses the same section here.
"""

import re

import attr

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
# Closing sequence: trailing #'s preceded by a space, e.g. "## Title ##".
_CLOSING_HASHES = re.compile(r"[ \t]+#+$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@attr.s(auto_attribs=True, frozen=True)
class Header:
    level: int
    text: str
    slug: str
    # 1-based line number of the heading, so it lines up with what an editor shows.
    line: int


def slugify_header(text: str) -> str:
    """GitHub-style slug. Accepts raw heading text with or without its ``#`` marks."""
    stripped = re.sub(r"^ {0,3}#+[ \t]*", "", text).lower()
    kept = "".join(c for c in stripped if c.isalpha() or c.isnumeric() or c.isspace() or c in "_-")
    return re.sub(r"\s+", "-", kept.strip())


def _heading_at(line: str) -> tuple[int, str] | None:
    match = _ATX.match(line)
    if match is None:
        return None
    return len(match.group(1)), _CLOSING_HASHES.sub("", match.group(2) or "").strip()


def _content_lines(markdown: str) -> list[tuple[int, str, tuple[int, str] | None]]:
    """Every line as ``(index, text, heading)``; ``heading`` is None inside fenced code blocks."""
    fence: str | None = None
    out: list[tuple[int, str, tuple[int, str] | None]] = []
    for index, line in enumerate(markdown.split("\n")):
        match = _FENCE.match(line)
        if fence is not None:
            if match is not None and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            out.append((index, line, None))
            continue
        if match is not None:
            fence = match.group(1)
            out.append((index, line, None))
            continue
        out.append((index, line, _heading_at(line)))
    return out


def parse_headers(markdown: str) -> list[Header]:
    """Headers in document order. Headings inside fenced code blocks are skipped."""
    return [
        Header(level=heading[0], text=heading[1], slug=slugify_header(heading[1]), line=index + 1)
        for index, _, heading in _content_lines(markdown)
        if heading is not None
    ]


def extract_section(markdown: str, header: str) -> str | None:
    """The heading line plus everything under it, up to the next heading of the same or higher level.

    ``header`` is a slug or raw heading text; the first heading that slugifies to it wins. Returns
    None if nothing matches. Trailing blank lines are dropped, but the text is otherwise verbatim.
    """
    target = slugify_header(header)
    if not target:
        return None

    lines = _content_lines(markdown)
    start: int | None = None
    level = 0
    end = len(lines)
    for index, _, heading in lines:
        if start is None:
            if heading is not None and slugify_header(heading[1]) == target:
                start, level = index, heading[0]
            continue
        if heading is not None and heading[0] <= level:
            end = index
            break
    if start is None:
        return None
    return re.sub(r"\n+$", "", "\n".join(line for _, line, _ in lines[start:end]))
