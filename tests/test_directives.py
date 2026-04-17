"""Tests for mm_bridge.directives — the VibeDeck directive parser."""

from __future__ import annotations

from mm_bridge.directives import Directive, extract


def test_single_open_file() -> None:
    cleaned, directives = extract('<openFile path="src/a.py" />')
    assert cleaned == ""
    assert directives == [Directive("open_file", {"path": "src/a.py"})]


def test_multiple_open_files_preserve_between_text() -> None:
    cleaned, directives = extract(
        'Check <openFile path="a.py"/> and <openFile path="b.py"/>'
    )
    assert cleaned == "Check  and "
    assert directives == [
        Directive("open_file", {"path": "a.py"}),
        Directive("open_file", {"path": "b.py"}),
    ]


def test_open_file_with_line_attr() -> None:
    cleaned, directives = extract('<openFile path="x.py" line="42" />')
    assert cleaned == ""
    assert len(directives) == 1
    assert directives[0].kind == "open_file"
    assert directives[0].attrs == {"path": "x.py", "line": "42"}


def test_leave_channel_without_reason() -> None:
    cleaned, directives = extract("bye <leaveChannel />")
    assert cleaned == "bye "
    assert directives == [Directive("leave_channel", {})]


def test_leave_channel_with_reason() -> None:
    cleaned, directives = extract('<leaveChannel reason="done" />')
    assert cleaned == ""
    assert directives == [Directive("leave_channel", {"reason": "done"})]


def test_mixed_directives_keep_order() -> None:
    cleaned, directives = extract(
        '<openFile path="a"/> text <leaveChannel reason="x"/>'
    )
    assert cleaned == " text "
    assert [d.kind for d in directives] == ["open_file", "leave_channel"]
    assert directives[0].attrs == {"path": "a"}
    assert directives[1].attrs == {"reason": "x"}


def test_no_directives_returns_original_text() -> None:
    cleaned, directives = extract("just text")
    assert cleaned == "just text"
    assert directives == []


def test_case_insensitive_tag_names() -> None:
    cleaned, directives = extract('<OPENFILE path="x"/>')
    assert cleaned == ""
    assert directives == [Directive("open_file", {"path": "x"})]


def test_case_insensitive_leave_channel() -> None:
    cleaned, directives = extract("<LEAVECHANNEL />")
    assert cleaned == ""
    assert directives == [Directive("leave_channel", {})]


def test_directive_inside_code_fence_still_matches() -> None:
    # Mirrors VibeDeck JS regex behavior — no code-fence awareness.
    text = '```\n<openFile path="a"/>\n```'
    cleaned, directives = extract(text)
    assert directives == [Directive("open_file", {"path": "a"})]
    assert "<openFile" not in cleaned
    assert cleaned.startswith("```")
    assert cleaned.endswith("```")


def test_extra_attrs_preserved() -> None:
    cleaned, directives = extract('<openFile path="a" follow="true"/>')
    assert cleaned == ""
    assert directives == [
        Directive("open_file", {"path": "a", "follow": "true"})
    ]


def test_open_file_with_no_attrs_is_not_filtered_here() -> None:
    # Regex for openFile requires \s+ before attrs so a truly empty
    # "<openFile/>" does NOT match — the JS reference has the same constraint.
    # But an openFile with whitespace-only inner content *does* match with
    # empty attrs, and the parser must not filter it (the caller checks path).
    cleaned, directives = extract("<openFile />")
    assert cleaned == ""
    assert directives == [Directive("open_file", {})]


def test_collapses_blank_line_runs_from_stripping() -> None:
    text = 'before\n\n<openFile path="a"/>\n\nafter'
    cleaned, directives = extract(text)
    assert directives == [Directive("open_file", {"path": "a"})]
    # The stripped directive leaves "before\n\n\n\nafter" — collapse to one blank.
    assert cleaned == "before\n\nafter"


def test_text_between_directives_preserved_verbatim() -> None:
    text = '<openFile path="a"/>middle<openFile path="b"/>'
    cleaned, directives = extract(text)
    assert cleaned == "middle"
    assert [d.attrs["path"] for d in directives] == ["a", "b"]
