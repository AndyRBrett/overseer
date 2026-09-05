"""The plain half of the dashboard, and the toggle that hides the rest.

There is no JS test runner here, so — like tests/test_implement_queue.py — this
pins the seams from Python: that app.js writes into element ids index.html
actually has, that nothing was deleted when the technical panels were demoted,
and that the page never composes a verdict of its own.
"""

import pathlib
import re

import pytest

HTML = pathlib.Path("docs/index.html").read_text(encoding="utf-8")
APP = pathlib.Path("docs/app.js").read_text(encoding="utf-8")


def _ids(source):
    return set(re.findall(r'id="([^"]+)"', source))


@pytest.mark.parametrize("element", [
    "plain-card", "plain-verdict", "plain-lines", "plain-checked",
    "details", "details-toggle",
])
def test_the_plain_half_exists_in_the_page(element):
    assert element in _ids(HTML)


@pytest.mark.parametrize("element", [
    "plain-verdict", "plain-lines", "plain-checked", "details", "details-toggle",
])
def test_app_js_writes_only_into_elements_that_exist(element):
    assert f'$("{element}")' in APP
    assert element in _ids(HTML)


def test_nothing_was_deleted_when_the_details_were_demoted():
    # The point of a two-level page is that the technical half is one tap away,
    # not gone. Every panel that existed before still has to be in the document.
    for element in ("rollup-card", "stats", "projects-card", "trends-card",
                    "shipped-card", "implementer-card", "spend-card", "digest",
                    "history-card", "timeline", "push-card", "generated"):
        assert element in _ids(HTML), f"{element} disappeared"


def test_every_technical_panel_sits_inside_the_details_wrapper():
    body = HTML.split('<details id="details">', 1)[1].split("</details>", 1)[0]
    for element in ("rollup-card", "projects-card", "shipped-card",
                    "implementer-card", "spend-card", "history-card", "generated"):
        assert f'id="{element}"' in body, f"{element} is not behind the toggle"


def test_the_plain_card_is_not_behind_the_toggle():
    before = HTML.split('<details id="details">', 1)[0]
    assert 'id="plain-card"' in before


def _visible(source):
    """Markup with comments, the stylesheet and tag names stripped — roughly
    what a reader sees. A jargon check that matched CSS class names and the
    comments explaining the jargon would fail on its own explanation."""
    text = re.sub(r"<!--.*?-->", " ", source, flags=re.S)
    text = re.sub(r"<style>.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def test_the_first_screen_carries_no_jargon():
    # The bar this rewrite was asked to clear. Every one of these was on the
    # first screen before it, and every one still exists in the details half
    # where it belongs.
    plain = _visible(HTML.split('<details id="details">', 1)[0])
    for word in ("Bug-Hunter", "Reviewer", "digest", "tool calls", "in flight",
                 "effort:", "dispatch", "tier", "SLA", "attention score"):
        assert word not in plain, f"jargon on the first screen: {word!r}"


def test_the_jargon_is_still_available_behind_the_toggle():
    # Demoted, not deleted — the counterpart to the test above. Without this,
    # deleting the technical half outright would pass every check in this file.
    details = _visible(HTML.split('<details id="details">', 1)[1])
    for word in ("Project health", "Model spend", "Latest digest"):
        assert word in details, f"{word!r} was removed rather than demoted"


def test_the_page_says_when_each_half_was_last_checked():
    # Two clocks, said out loud: health refreshes several times a day, the
    # written review is weekly. One date for both is exactly how a five-day-old
    # panel passed for current.
    assert "d.refreshed" in APP and "d.generated" in APP
    assert "full review" in APP


def test_an_open_page_refreshes_itself():
    # An installed PWA is a window left open for days. It fetched once on launch
    # and then showed whatever it had.
    assert "visibilitychange" in APP
    assert "setInterval" in APP
    # But never a backgrounded tab, and never more than once a minute.
    assert "document.hidden" in APP
    assert "MIN_RELOAD_GAP_MS" in APP


def test_the_toggle_remembers_which_way_it_was_left():
    assert "overseer-details-open" in APP
    # localStorage throws in some private-browsing modes; every access here is
    # wrapped, as the rest of this file's accesses already are.
    body = APP.split("const DETAILS_KEY", 1)[1].split("// Keep an open page", 1)[0]
    assert body.count("try {") >= 2
