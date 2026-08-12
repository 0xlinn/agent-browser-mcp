"""optimize_html_for_tokens: shrink the HTML without making links unreachable.

Long hrefs used to become a bare '__link__', so on a search results page the
agent could read 30 titles and reach none of them. They now become short '#rN'
refs with the real (absolute) URL handed back out of band.
"""
from agent_browser_mcp.simphtml import optimize_html_for_tokens

LONG = "https://linux.do/t/some-long-topic-slug-here/2725484"
SHORT = "/short"


def render(html, **kw):
    return str(optimize_html_for_tokens(html, **kw))


class TestLegacyBehaviour:
    def test_without_link_refs_it_still_placeholders(self):
        # Callers that don't want refs (text_only) keep the old cheap behaviour.
        out = render(f'<a href="{LONG}">x</a>')
        assert "__link__" in out
        assert LONG not in out

    def test_short_hrefs_are_never_touched(self):
        out = render(f'<a href="{SHORT}">x</a>')
        assert SHORT in out


class TestRefs:
    def test_long_href_becomes_a_ref(self):
        refs = {}
        out = render(f'<a href="{LONG}">x</a>', link_refs=refs)
        assert 'href="#r1"' in out
        assert "__link__" not in out
        assert refs == {LONG: "r1"}

    def test_same_url_reuses_one_ref(self):
        refs = {}
        out = render(f'<a href="{LONG}">a</a><a href="{LONG}">b</a>', link_refs=refs)
        assert out.count('href="#r1"') == 2
        assert len(refs) == 1

    def test_distinct_urls_get_distinct_refs(self):
        refs = {}
        html = "".join(f'<a href="{LONG}?p={i}">x</a>' for i in range(3))
        out = render(html, link_refs=refs)
        assert len(refs) == 3
        assert len(set(refs.values())) == 3
        for ref in refs.values():
            assert f'href="#{ref}"' in out

    def test_every_ref_in_html_is_resolvable(self):
        import re

        refs = {}
        html = "".join(f'<a href="{LONG}/{i}">x</a>' for i in range(5))
        out = render(html, link_refs=refs)
        used = set(re.findall(r'href="#(r\d+)"', out))
        assert used == set(refs.values())


class TestAbsoluteResolution:
    """A relative ref is not something open_url can navigate to.

    Caught by the live suite: MDN and GitHub return hrefs like
    '/en-US/docs/Learn_web_development', which are long enough to be replaced
    but useless to the agent unless resolved against the page URL.
    """

    BASE = "https://developer.mozilla.org/en-US/docs/Web/API/Window/scrollY"

    def test_relative_path_becomes_absolute(self):
        refs = {}
        render('<a href="/en-US/docs/Learn_web_development_and_more">x</a>',
               link_refs=refs, base_url=self.BASE)
        (url,) = refs
        assert url == "https://developer.mozilla.org/en-US/docs/Learn_web_development_and_more"

    def test_absolute_href_is_unchanged(self):
        refs = {}
        render(f'<a href="{LONG}">x</a>', link_refs=refs, base_url=self.BASE)
        assert list(refs) == [LONG]

    def test_all_refs_are_absolute_when_base_given(self):
        refs = {}
        render(
            '<a href="/en-US/docs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">a</a>'
            '<a href="../relative/bbbbbbbbbbbbbbbbbbbbbbbbbbbbb">b</a>'
            f'<a href="{LONG}">c</a>',
            link_refs=refs, base_url=self.BASE)
        assert len(refs) == 3
        assert all(u.startswith("http") for u in refs)

    def test_no_base_url_keeps_the_raw_href(self):
        refs = {}
        render('<a href="/en-US/docs/Learn_web_development_and_more">x</a>',
               link_refs=refs)
        assert list(refs) == ["/en-US/docs/Learn_web_development_and_more"]

    def test_unparseable_base_does_not_raise(self):
        refs = {}
        render('<a href="/some/long/relative/path/that/is/long">x</a>',
               link_refs=refs, base_url="::::not-a-url::::")
        assert len(refs) == 1


class TestOtherAttributes:
    def test_data_uri_src_is_collapsed(self):
        out = render('<img src="data:image/png;base64,AAAA">')
        assert "__img__" in out

    def test_long_src_is_collapsed(self):
        out = render(f'<img src="{LONG}">')
        assert "__url__" in out

    def test_long_action_is_collapsed(self):
        out = render(f'<form action="{LONG}"></form>')
        assert "__url__" in out

    def test_refs_do_not_leak_into_src(self):
        # Only href participates in the ref scheme; src has no navigation use.
        refs = {}
        render(f'<img src="{LONG}">', link_refs=refs)
        assert refs == {}
