"""Real Chromium fixture proof, explicitly not assembled-service proof."""

import time

from playwright.sync_api import expect, sync_playwright

from tests.task_acceptance_support.site import synthetic_site


def eventually_count(site, action, count):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if site.counters().get(action, 0) == count:
            return
        time.sleep(.01)
    assert site.counters().get(action, 0) == count


def test_real_browser_private_state_reopen_and_separate_contexts(tmp_path):
    with synthetic_site(tmp_path) as site, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        owner = browser.new_context()
        page = owner.new_page()
        page.goto(site.url)
        page.locator('#name').fill('synthetic-owner')
        page.locator('#login').click()
        eventually_count(site, 'login', 1)
        saved = owner.storage_state()
        for _ in range(2):  # same-account second profile and different account context
            other = browser.new_context()
            tab = other.new_page()
            tab.goto(site.url)
            assert tab.evaluate('localStorage.getItem("synthetic_user")') is None
            assert other.cookies() == []
            other.close()
        owner.close()
        restored = browser.new_context(storage_state=saved)
        tab = restored.new_page()
        tab.goto(site.url)
        assert tab.evaluate('localStorage.getItem("synthetic_user")') == 'synthetic-owner'
        assert any(cookie['name'] == 'synthetic_session' for cookie in restored.cookies())
        assert site.counters()['login'] == 1
        browser.close()


def test_real_browser_selector_delay_frame_drag_keyboard_and_counters(tmp_path):
    with synthetic_site(tmp_path) as site, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(site.url)
        page.locator('#prerequisite').click()
        eventually_count(site, 'prerequisite', 1)
        old = page.query_selector('#add-original')
        site.selector = 'add-repaired'
        page.reload()
        # Detached references must fail, not silently hit a new target.
        import pytest
        from playwright.sync_api import Error
        with pytest.raises(Error):
            old.click(timeout=200)
        page.locator('[data-action=cart]').click()
        eventually_count(site, 'cart', 1)
        before = page.url
        page.locator('#delay').click()
        expect(page.locator('#state')).to_have_text('settled')
        assert page.url == before
        page.locator('#late').click()
        page.locator('#name').click()
        page.keyboard.type('local-fixture')
        page.keyboard.press('Tab')
        page.drag_and_drop('#drag', '#drop')
        page.frame_locator('iframe').locator('#frame').click()
        eventually_count(site, 'drag', 1)
        eventually_count(site, 'frame', 1)
        page.locator('#navigate').click()
        assert page.url.endswith('/next')
        assert site.counters()['prerequisite'] == 1
        assert site.counters().get('submit', 0) == 0
        browser.close()


def test_real_browser_effect_committed_before_lost_response_never_replayed_by_fixture(tmp_path):
    with synthetic_site(tmp_path) as site, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(site.url)
        site.hold_response = True
        page.locator('[data-action=cart]').click()
        assert site.after_effect.wait(3), 'durable effect not observed'
        assert site.counters() == {'cart': 1}
        browser.close()  # Lose the response after the SQLite website effect committed.
        site.release_response.set()
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(site.url)
        assert site.counters() == {'cart': 1}
        browser.close()
