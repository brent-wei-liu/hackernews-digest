"""End-to-end Playwright UI regression tests for hackernews-digest.

Hits the real /api endpoints. Tests skip cleanly if there isn't enough
seed data for a given assertion to be meaningful (no stories yet, no
digests yet, etc.) — useful early in setup before the first cron has
populated anything.
"""
import re

import pytest
from playwright.sync_api import Page, expect


def _open(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")
    # Wait for either the loader's replacement or an empty state to land
    page.wait_for_function(
        "() => { const c = document.getElementById('canvas');"
        " return c && (c.querySelector('.story') || c.querySelector('.digest-row') "
        "  || c.querySelector('.empty')); }",
        timeout=10_000,
    )


# ---------------------------------------------------------------------
# A. Tab switching — STORIES ↔ DIGESTS
# ---------------------------------------------------------------------

def test_tab_switch(page: Page, base_url: str):
    _open(page, base_url)
    expect(page.locator(".tab[data-tab='stories']")).to_have_class(re.compile(r"\bactive\b"))

    page.locator(".tab[data-tab='digests']").click()
    expect(page.locator(".tab[data-tab='digests']")).to_have_class(re.compile(r"\bactive\b"), timeout=2_000)
    page.wait_for_function(
        "() => document.getElementById('canvas').querySelector('.digest-row, .empty')",
        timeout=5_000,
    )

    page.locator(".tab[data-tab='stories']").click()
    expect(page.locator(".tab[data-tab='stories']")).to_have_class(re.compile(r"\bactive\b"), timeout=2_000)


# ---------------------------------------------------------------------
# B. Sub-tabs — TOP / BEST / ALL switching fires a new /api/stories
# ---------------------------------------------------------------------

def test_sub_tab_list_switch(page: Page, base_url: str):
    _open(page, base_url)
    with page.expect_response(
        lambda r: "/api/stories" in r.url and "list_type=beststories" in r.url,
        timeout=5_000,
    ):
        page.locator(".sub[data-list='beststories']").click()
    expect(page.locator(".sub[data-list='beststories']")).to_have_class(re.compile(r"\bactive\b"))


# ---------------------------------------------------------------------
# C. Star toggle — persists across reload (when a story exists)
# ---------------------------------------------------------------------

def test_story_star_persists(page: Page, base_url: str):
    _open(page, base_url)
    if page.locator(".story").count() == 0:
        pytest.skip("no stories visible")

    first = page.locator(".story").first
    sid = first.get_attribute("data-id")
    star = first.locator(".star-btn")
    was_on = "on" in (star.get_attribute("class") or "")
    star.click()
    page.wait_for_function(
        "(args) => { const b = document.querySelector(`.story[data-id='${args.id}'] .star-btn`);"
        " return b && (b.classList.contains('on') !== args.was); }",
        arg={"id": sid, "was": was_on}, timeout=3_000,
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "(id) => document.querySelector(`.story[data-id='${id}'] .star-btn`)",
        arg=sid, timeout=10_000,
    )
    post_on = "on" in (page.locator(f".story[data-id='{sid}'] .star-btn").get_attribute("class") or "")
    assert post_on != was_on, "star state did not persist across reload"

    # Cleanup
    page.locator(f".story[data-id='{sid}'] .star-btn").click()


# ---------------------------------------------------------------------
# D. Search filter
# ---------------------------------------------------------------------

def test_search_filter(page: Page, base_url: str):
    _open(page, base_url)
    if page.locator(".story").count() == 0:
        pytest.skip("no stories visible")

    first_title = page.locator(".story .title a").first.inner_text().strip()
    tokens = [t for t in re.split(r"\W+", first_title) if len(t) >= 4]
    if not tokens:
        pytest.skip("no usable token in first title")
    term = tokens[0]

    with page.expect_response(
        lambda r: "/api/stories" in r.url and f"q={term.lower()}" in r.url.lower(),
        timeout=5_000,
    ):
        page.fill("#search-input", term)


# ---------------------------------------------------------------------
# E. Digest drawer opens with rendered HTML
# ---------------------------------------------------------------------

def test_digest_drawer_opens(page: Page, base_url: str):
    _open(page, base_url)
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests in DB")

    page.locator(".digest-row .open-btn").first.click()
    expect(page.locator("#modal-backdrop.open")).to_be_visible(timeout=3_000)
    page.wait_for_function(
        "() => { const b = document.getElementById('modal-body');"
        " return b && b.innerText && b.innerText.length > 50 && !b.querySelector('.loader'); }",
        timeout=5_000,
    )
    rendered = page.locator("#modal-body").evaluate(
        "(el) => Boolean(el.querySelector('h1, h2, h3, p, ul, strong'))"
    )
    assert rendered, "modal-body has no rendered HTML elements — looks like raw markdown"
    page.locator("#modal-close").click()
    expect(page.locator("#modal-backdrop.open")).not_to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------
# F. Read/unread mark — opening auto-marks; toggle reverses
# ---------------------------------------------------------------------

def test_digest_mark_read_persists(page: Page, base_url: str):
    _open(page, base_url)
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests")

    first = page.locator(".digest-row").first
    digest_id = first.get_attribute("data-id")
    # Force unread starting state
    page.evaluate(
        "(id) => fetch('/api/digests/' + id + '/unread', {method:'POST'})",
        arg=digest_id,
    )
    page.reload(wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    page.wait_for_selector(f".digest-row[data-id='{digest_id}']", timeout=5_000)
    row = page.locator(f".digest-row[data-id='{digest_id}']")
    expect(row).not_to_have_class(re.compile(r"\bis-read\b"))

    # Open via the OPEN button → auto-mark-read
    row.locator(".open-btn").click()
    page.wait_for_function(
        "(id) => document.querySelector(`.digest-row[data-id='${id}']`)?.classList.contains('is-read')",
        arg=digest_id, timeout=5_000,
    )

    # Toggle back to unread via the modal button
    btn = page.locator("#modal-read-toggle")
    expect(btn).to_be_visible(timeout=2_000)
    btn.click()
    expect(btn).to_have_text(re.compile(r"MARK READ", re.I), timeout=3_000)
    final = page.evaluate(
        "async (id) => (await (await fetch('/api/digests/' + id)).json()).is_read",
        arg=digest_id,
    )
    assert final == 0


# ---------------------------------------------------------------------
# G. Back-to-top — modal scroll → button fades in → click resets
# ---------------------------------------------------------------------

def test_digest_back_to_top(page: Page, base_url: str):
    _open(page, base_url)
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests")

    page.locator(".digest-row .open-btn").first.click()
    page.wait_for_function(
        "() => { const b = document.getElementById('modal-body');"
        " return b && b.innerText && b.innerText.length > 50; }",
        timeout=5_000,
    )
    btn = page.locator("#modal-top-btn")
    max_scroll = page.evaluate(
        "const b = document.getElementById('modal-backdrop'); b.scrollHeight - b.clientHeight"
    )
    if max_scroll < 400:
        pytest.skip(f"digest too short to test back-to-top ({max_scroll}px)")
    expect(btn).not_to_have_class(re.compile(r"\bvisible\b"))
    page.evaluate(
        "const b = document.getElementById('modal-backdrop'); "
        "b.scrollTo({top: Math.min(b.scrollHeight - b.clientHeight, 600), behavior: 'instant'});"
    )
    page.wait_for_function(
        "() => document.getElementById('modal-top-btn').classList.contains('visible')",
        timeout=2_000,
    )
    btn.click()
    page.wait_for_function(
        "() => document.getElementById('modal-backdrop').scrollTop < 10",
        timeout=2_000,
    )


# ---------------------------------------------------------------------
# H. Story link targets real HN urls (catches placeholder/test rows)
# ---------------------------------------------------------------------

def test_story_links_target_blank(page: Page, base_url: str):
    _open(page, base_url)
    if page.locator(".story").count() == 0:
        pytest.skip("no stories")
    # Check first 3 stories' [hn] sublinks point at news.ycombinator.com
    for i in range(min(3, page.locator(".story").count())):
        a = page.locator(".story").nth(i).locator(".meta a.hn-link")
        target = a.get_attribute("target")
        rel = a.get_attribute("rel") or ""
        href = a.get_attribute("href") or ""
        assert target == "_blank"
        assert "noopener" in rel
        assert href.startswith("https://news.ycombinator.com/item?id="), (
            f"hn-link href doesn't look like an HN url: {href!r}"
        )
