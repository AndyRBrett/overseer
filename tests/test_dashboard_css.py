"""Guards for the dashboard layout bugs that shipped to a phone screen.

There is no JS test runner in this repo, and these were CSS one-liners with
visible consequences: "$0.31" rendered as "$0.3 / 1" and "DUPLICATE" as
"DUPLICA / TE". Grepping the stylesheet is a blunt instrument, but it pins the
exact rules whose absence caused the damage — which beats finding out again
from a screenshot.
"""

import pathlib
import re

import pytest

CSS_RAW = pathlib.Path("docs/index.html").read_text(encoding="utf-8")
# Comments explain these very rules and name the properties they warn against,
# so matching against them would assert on prose rather than on the stylesheet.
CSS = re.sub(r"/\*.*?\*/", "", CSS_RAW, flags=re.S)


def _rule(selector):
    """The declaration block for a selector, or None."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else None


def test_body_does_not_break_words_at_arbitrary_characters():
    # `overflow-wrap: anywhere` on body was the cause: it breaks mid-token even
    # when the whole word would fit on the next line.
    body = _rule("body")
    assert body is not None
    assert "anywhere" not in body, "body must not use overflow-wrap: anywhere"
    assert "overflow-wrap: break-word" in body


def test_stat_numbers_can_never_wrap():
    n = _rule(".stat .n")
    assert n is not None
    assert "white-space: nowrap" in n, "a wrapped currency figure is not a number"


def test_stat_labels_break_between_words_only():
    label = _rule(".stat .l")
    assert label is not None
    assert "word-break: normal" in label
    assert "hyphens: none" in label


def test_stat_tiles_are_a_reflowing_grid_not_squeezed_flex():
    # flex:1 + min-width:70px is what crushed six tiles onto one row and forced
    # every label to wrap.
    stats = _rule(".stats")
    assert stats is not None
    assert "grid-template-columns" in stats and "auto-fit" in stats
    assert "min-width: 70px" not in (_rule(".stat") or "")


def test_unbreakable_token_handling_still_exists_somewhere():
    # The global rule was removed for good reason, but URLs and JSON payloads in
    # the timeline still need it — dropping it entirely would trade one overflow
    # bug for another.
    assert ".wrap-any" in CSS
    anywhere = _rule(".wrap-any, .digest, .item .body, pre, code")
    assert anywhere is not None and "anywhere" in anywhere


@pytest.mark.parametrize("cls", ["s-shipped", "s-flight", "s-open", "s-noplan"])
def test_every_delivery_bar_segment_has_a_colour(cls):
    # A segment with no background renders as an invisible gap in the bar, which
    # would silently misreport the delivery mix.
    assert f".dbar .{cls}" in CSS
    assert f".dkey .{cls}" in CSS
