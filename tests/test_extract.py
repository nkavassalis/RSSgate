from rssgate.extract import extract_article_text, extract_candidate_links, page_title

PAGE = """
<html><head><title>Test Article — Example Site</title></head><body>
<header><a href="/">Home</a><nav><a href="/about">About</a></nav></header>
<div class="ad banner" id="sponsor-ad">Buy our product today!! Click here to subscribe.</div>
<article>
  <h1>Test Article</h1>
  <p>The important sentence about the research findings, containing the key facts.</p>
  <div class="newsletter-signup">Sign up for our daily newsletter!</div>
  <p>Second solid paragraph with more substance that a reader actually cares about.</p>
  <div class="related-content"><p>You might also like: celebrity gossip roundup.</p></div>
  <blockquote>"A key quotation from the lead author of the study."</blockquote>
</article>
<footer>Copyright 2026. Cookie settings. <div class="ads">ad xyz</div></footer>
</body></html>
"""


def test_keeps_article_text():
    text = extract_article_text(PAGE)
    assert "important sentence about the research findings" in text
    assert "Second solid paragraph" in text
    assert "key quotation" in text


def test_drops_ads_nav_and_chrome():
    text = extract_article_text(PAGE)
    assert "Buy our product" not in text
    assert "newsletter" not in text.lower()
    assert "celebrity gossip" not in text
    assert "Copyright" not in text


def test_headings_marked():
    assert "## Test Article" in extract_article_text(PAGE)


def test_never_strips_body_even_with_junk_classes():
    tricky = ('<html><body class="single embed post-template-default"><article>'
              '<p>Body survives despite the embed class on the body tag, long enough.</p>'
              '</article></body></html>')
    assert "Body survives" in extract_article_text(tricky)


def test_page_title():
    assert page_title(PAGE) == "Test Article — Example Site"


def test_picks_article_with_real_body_not_widget_cards():
    multi = """<html><body>
    <article class="card"><a>Trending</a></article>
    <article class="card"><a>More from us</a></article>
    <article class="post"><div class="entry-content">
      <p>Real body paragraph one with plenty of substance to matter to readers.</p>
      <p>Real body paragraph two with plenty of substance to matter to readers.</p>
    </div></article></body></html>"""
    text = extract_article_text(multi)
    assert "Real body paragraph one" in text
    assert "Trending" not in text


def test_candidate_links_same_domain_and_min_length():
    html = """<html><body>
    <a href="https://blog.example.com/posts/this-is-a-real-article-about-things">
        This Is A Real Article About Things</a>
    <a href="https://blog.example.com/tag">Tag</a>
    <a href="https://other.com/posts/long-enough-cross-domain-article-title">cross</a>
    <a href="/posts/relative-link-with-a-fairly-long-anchor-text">Relative Article Text Here</a>
    </body></html>"""
    links = {l["link"] for l in extract_candidate_links(html, "https://blog.example.com/")}
    assert "https://blog.example.com/posts/this-is-a-real-article-about-things" in links
    assert any(l.endswith("/posts/relative-link-with-a-fairly-long-anchor-text") for l in links)
    assert not any("other.com" in l for l in links)   # cross-domain dropped
    assert not any(l.endswith("/tag") for l in links)  # short anchor dropped
