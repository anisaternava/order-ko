"""Read a confirmation through a signed-in, visible Outlook browser session."""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


def reference_present(text: str, reference: str) -> bool:
    return bool(reference and re.search(r"(?<!\w)" + re.escape(reference) + r"(?!\w)", text, re.I))


def setup_profile(profile: Path, mail_url: str) -> None:
    profile.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(mail_url, wait_until="domcontentloaded")
        print("Sign in to Outlook in this browser window, then press Enter here. No password is read by the tool.")
        input()
        context.close()


def _searchbox(page):
    for selector in ('input[role="searchbox"]', 'input[aria-label*="Search"]', 'input[placeholder*="Search"]'):
        locator = page.locator(selector)
        if locator.count() and locator.first.is_visible():
            return locator.first
    return None


def inspect_mail(profile: Path, config: dict, reference: str, recipient: str,
                 since_timestamp: float, download_dir: Path, cdp_url: str = "") -> dict:
    """Search exact order reference; return only data exposed by the visible UI."""
    if not reference:
        return {"status": "EMAIL_NOT_CHECKED", "reason": "No confirmed order reference was captured"}
    if not cdp_url and not profile.exists():
        return {"status": "EMAIL_NOT_CHECKED", "reason": "Run --setup-mail-browser to sign in to a reusable browser profile"}
    download_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            if cdp_url:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0]
                page = next((item for item in context.pages if "outlook" in item.url.lower()), None)
                if page is None:
                    page = context.new_page()
            else:
                context = playwright.chromium.launch_persistent_context(str(profile), headless=False,
                                                                         accept_downloads=True)
                page = context.pages[0] if context.pages else context.new_page()
            page.goto(config.get("url", "https://outlook.live.com/mail/"), wait_until="domcontentloaded")
            box = _searchbox(page)
            if box is None:
                return {"status": "EMAIL_NOT_CHECKED", "reason": "Signed-in Outlook search control was unavailable"}
            deadline = time.monotonic() + max(5, int(config.get("wait_seconds", 120)))
            while time.monotonic() < deadline:
                box.fill(reference)
                box.press("Enter")
                page.wait_for_timeout(2500)
                rows = [row for row in page.get_by_role("row").all()
                        if row.is_visible() and reference_present(row.inner_text(), reference)]
                if len(rows) == 1:
                    rows[0].click()
                    page.wait_for_timeout(1200)
                    visible = page.locator("body").inner_text()
                    if not reference_present(visible, reference):
                        return {"status": "REVIEW", "reason": "Opened message did not show the exact order reference"}
                    if recipient and recipient.casefold() not in visible.casefold():
                        return {"status": "REVIEW", "reason": "Recipient was not visible in the opened message"}
                    fields = {}
                    for name, pattern in config.get("field_patterns", {}).items():
                        if re.search(r"card|cvv|cvc|payment|password|token", name, re.I):
                            continue
                        match = re.search(pattern, visible, re.I | re.M)
                        if match:
                            fields[name] = (match.group("value") if "value" in match.groupdict()
                                            else match.group(1) if match.lastindex else match.group()).strip()
                    attachment = None
                    documents = {}
                    pattern = config.get("plan_attachment_pattern", "")
                    if pattern:
                        links = [item for item in page.get_by_role("button").all() + page.get_by_role("link").all()
                                 if item.is_visible() and re.search(pattern, item.inner_text(), re.I)]
                        if len(links) == 1:
                            try:
                                with page.expect_download(timeout=10000) as download_info:
                                    links[0].click()
                                download = download_info.value
                                suffix = Path(download.suggested_filename).suffix.lower()
                                if suffix in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
                                    attachment = download_dir / f"email-plan{suffix}"
                                    download.save_as(attachment)
                            except PlaywrightTimeout:
                                pass
                    for document_name, filename_pattern in config.get("attachment_rules", {}).items():
                        if not filename_pattern:
                            continue
                        links = [item for item in page.get_by_role("button").all() + page.get_by_role("link").all()
                                 if item.is_visible() and re.search(filename_pattern, item.inner_text(), re.I)]
                        if len(links) == 1:
                            try:
                                with page.expect_download(timeout=10000) as download_info:
                                    links[0].click()
                                documents[document_name] = bool(download_info.value.suggested_filename)
                            except PlaywrightTimeout:
                                documents[document_name] = False
                    return {"status": "PASS", "reason": "Exact order reference and recipient visible in opened message",
                            "fields": fields, "attachment": attachment, "documents": documents,
                            "visible_text": visible}
                if len(rows) > 1:
                    return {"status": "REVIEW", "reason": "More than one search result contains this order reference"}
                page.wait_for_timeout(5000)
            return {"status": "EMAIL_NOT_CHECKED", "reason": "No exact order-reference email appeared before the wait period ended"}
        except Exception as exc:
            return {"status": "EMAIL_NOT_CHECKED", "reason": f"Browser mailbox could not be inspected: {type(exc).__name__}"}
        finally:
            if context and not cdp_url:
                context.close()
            if browser:
                browser.close()
