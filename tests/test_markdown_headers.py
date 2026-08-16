from server.core.markdown_headers import extract_section
from server.core.markdown_headers import parse_headers
from server.core.markdown_headers import slugify_header

DOC = """\
# Title

Intro text.

## Plan

Ship it.

### Later

Maybe.

## Notes & Ideas

Fin.
"""


def test_slugify_matches_github_style() -> None:
    assert slugify_header("## Notes & Ideas") == "notes-ideas"
    assert slugify_header("Hello, World!") == "hello-world"
    assert slugify_header("keep_under-score") == "keep_under-score"
    assert slugify_header("  spaced   out  ") == "spaced-out"


def test_parse_headers_in_document_order() -> None:
    assert [(h.level, h.text, h.slug, h.line) for h in parse_headers(DOC)] == [
        (1, "Title", "title", 1),
        (2, "Plan", "plan", 5),
        (3, "Later", "later", 9),
        (2, "Notes & Ideas", "notes-ideas", 13),
    ]


def test_headers_inside_code_fences_are_skipped() -> None:
    doc = "# Real\n\n```\n# Not a header\n```\n\n~~~md\n## Also not\n~~~\n\n## Real too\n"
    assert [h.text for h in parse_headers(doc)] == ["Real", "Real too"]


def test_closing_hashes_are_not_part_of_the_text() -> None:
    assert [h.text for h in parse_headers("## Title ##\n")] == ["Title"]


def test_extract_section_includes_subheaders_and_stops_at_the_next_peer() -> None:
    assert extract_section(DOC, "plan") == "## Plan\n\nShip it.\n\n### Later\n\nMaybe."


def test_extract_section_runs_to_the_end_of_the_document() -> None:
    assert extract_section(DOC, "notes-ideas") == "## Notes & Ideas\n\nFin."


def test_extract_section_accepts_raw_heading_text() -> None:
    assert extract_section(DOC, "Notes & Ideas") == extract_section(DOC, "notes-ideas")


def test_extract_top_level_section_takes_the_whole_document() -> None:
    assert extract_section(DOC, "title") == DOC.rstrip("\n")


def test_extract_section_returns_none_when_missing() -> None:
    assert extract_section(DOC, "nope") is None
    assert extract_section(DOC, "") is None


def test_extract_section_leaves_body_text_verbatim() -> None:
    doc = "## A\n\nline with trailing spaces  \n\ttabbed\n\n## B\n"
    assert extract_section(doc, "a") == "## A\n\nline with trailing spaces  \n\ttabbed"
