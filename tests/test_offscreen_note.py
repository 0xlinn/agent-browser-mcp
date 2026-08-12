"""_offscreen_note: report what the viewport clamp dropped.

optHTML excludes anything more than 5000px from the CURRENT scroll offset. That
content used to vanish with no trace, so "not on this page" and "not scrolled to
it yet" looked identical to the agent. optHTML now emits a marker comment and
scan_page turns it into a field plus a hint.
"""
from agent_browser_mcp.server import _offscreen_note

MARKER = "<!--tmwd-offscreen:15 scrollY:0 viewH:780 docH:2574-->"


class TestParsing:
    def test_marker_is_parsed(self):
        note = _offscreen_note(f"<body>{MARKER}<div>hi</div></body>")
        assert note == {
            "elements": 15,
            "scroll_y": 0,
            "viewport_height": 780,
            "doc_height": 2574,
        }

    def test_marker_anywhere_in_the_document(self):
        assert _offscreen_note(f"<html><body><div>x</div>{MARKER}</body></html>")

    def test_negative_scroll_offset(self):
        # Overscroll / rubber-banding can report a negative scrollY.
        note = _offscreen_note("<!--tmwd-offscreen:3 scrollY:-120 viewH:800 docH:4000-->")
        assert note["scroll_y"] == -120

    def test_large_counts(self):
        note = _offscreen_note(
            "<!--tmwd-offscreen:12345 scrollY:99999 viewH:1080 docH:250000-->")
        assert note["elements"] == 12345
        assert note["doc_height"] == 250000


class TestAbsent:
    def test_no_marker_means_nothing_was_dropped(self):
        assert _offscreen_note("<body><div>everything fit</div></body>") is None

    def test_non_string_content(self):
        # text_only=True returns a str, but a caller could pass anything.
        assert _offscreen_note(None) is None
        assert _offscreen_note(1234) is None
        assert _offscreen_note({"content": MARKER}) is None

    def test_empty_string(self):
        assert _offscreen_note("") is None

    def test_malformed_marker_is_ignored_not_crashed(self):
        assert _offscreen_note("<!--tmwd-offscreen:oops-->") is None
        assert _offscreen_note("<!--tmwd-offscreen:5 scrollY:-->") is None

    def test_similar_looking_text_does_not_match(self):
        assert _offscreen_note("tmwd-offscreen:15 but not a comment") is None
