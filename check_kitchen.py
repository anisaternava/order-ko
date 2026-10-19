"""Public Fragmento journey checker; stops before paid order submission."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import msal
import requests
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
MAIL_CACHE = ROOT / ".auth" / "outlook_token_cache.json"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    screenshot: str = ""


@dataclass
class Run:
    kitchen: str
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())
    checks: list[Check] = field(default_factory=list)
    selection: dict[str, str] = field(default_factory=dict)
    summary: dict[str, str] = field(default_factory=dict)
    email: dict[str, str] = field(default_factory=dict)
    reference: str = ""
    plan_url: str = ""
    expected_documents: dict[str, list[str]] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "", screenshot: str = "") -> None:
        self.checks.append(Check(name, status, detail, screenshot))


def configured(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def label(config: dict, key: str, default: str) -> str:
    return config.get("labels", {}).get(key, default)


def read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError("Workflow configuration must be a JSON object")
    return config


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.casefold().split())


def comparable(name: str, value: str) -> str:
    """Normalize presentation only for known numeric fields."""
    if name in {"subtotal", "vat", "total_price"} or name.endswith(":price"):
        compact = value.replace("€", "").replace("\u00a0", "").replace(" ", "").strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", compact):
            compact = compact.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d+,\d{2}", compact):
            compact = compact.replace(",", ".")
        try:
            return str(Decimal(compact).quantize(Decimal("0.01")))
        except InvalidOperation:
            pass
    if name.endswith(":quantity"):
        try:
            return str(int(value.strip()))
        except ValueError:
            pass
    return normalized(value)


def exact_reference(text: str, reference: str) -> bool:
    if not reference:
        return False
    return re.search(r"(?<![\w])" + re.escape(reference) + r"(?![\w])", text, re.I) is not None


def extract(pattern: str, text: str) -> str:
    if not configured(pattern):
        return ""
    match = re.search(pattern, text, re.I | re.M)
    if not match:
        return ""
    if "value" in match.groupdict():
        return match.group("value").strip()
    return (match.group(1) if match.lastindex else match.group(0)).strip()


def check_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def screenshot(page, run: Run, label: str) -> str:
    folder = REPORTS / f"{run.started:%Y%m%d-%H%M%S-%f}-{run.kitchen}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{label}.png"
    page.screenshot(path=str(target), full_page=True)
    return str(target.relative_to(REPORTS)).replace("\\", "/")


def check_images(page, selector: str, run: Run, source: str) -> None:
    if not configured(selector):
        run.add(f"{source} images", "UNCONFIGURED", "Image selector is missing")
        return
    try:
        images = page.locator(selector)
        count = images.count()
        if not count:
            run.add(f"{source} images", "FAIL", "No images found", screenshot(page, run, f"{source}-images"))
            return
        broken = []
        for index in range(count):
            image = images.nth(index)
            image.scroll_into_view_if_needed(timeout=10000)
            image.evaluate("el => el.decode ? el.decode().catch(() => {}) : Promise.resolve()")
            good = image.evaluate("el => el.complete && el.naturalWidth > 0 && el.naturalHeight > 0")
            if not good:
                broken.append(index + 1)
        run.add(f"{source} images", "FAIL" if broken else "PASS",
                f"{count} checked; broken positions: {broken}" if broken else f"{count} loaded",
                screenshot(page, run, f"{source}-images"))
    except PlaywrightError as exc:
        run.add(f"{source} images", "FAIL", f"Image check failed: {type(exc).__name__}", screenshot(page, run, f"{source}-images"))


def public_contract_check(page, config: dict, run: Run, interactive: bool = False) -> None:
    """Follow the observed public contract entry and pre-submit order flow."""
    required = ("url", "contract_input", "confirm_button", "plan_image")
    if any(not configured(config.get(key)) for key in required) or not check_url(config.get("url", "")):
        run.add("Public kitchen", "UNCONFIGURED", "Configure the observed contract entry flow")
        return
    try:
        page.goto(config["url"], wait_until="networkidle", timeout=30000)
        for label in ("Nur notwendige", "Only necessary", "Necessary only"):
            choice = page.get_by_text(label, exact=True)
            if choice.count() and choice.first.is_visible():
                choice.first.click()
                break
        contract_input = page.locator(config["contract_input"])
        if not contract_input.count():
            language = page.get_by_text(config.get("language", "English"), exact=True)
            if language.count() and language.first.is_visible():
                language.first.click()
            text_button = page.get_by_role("button", name=config.get("instruction_label", "Text"), exact=True)
            if text_button.count() and text_button.first.is_visible():
                text_button.first.click()
            continue_button = page.get_by_role("button", name=config.get("intro_continue_label", "Continue"), exact=True)
            if continue_button.count() and continue_button.first.is_visible():
                continue_button.first.click()
        contract_input.fill(run.kitchen, timeout=10000)
        page.locator(config["confirm_button"]).click(timeout=10000)
        page.wait_for_url(re.compile(r"/kitchens/[^/?]+"), timeout=30000)
        parsed = urlparse(page.url)
        actual = parse_qs(parsed.query).get("contractNumber", [""])[0]
        if actual != run.kitchen:
            run.add("Public kitchen", "FAIL", f"Opened page has different contract number: {actual}", screenshot(page, run, "public-wrong-kitchen"))
            return
        slug = parsed.path.rstrip("/").split("/")[-1]
        expected = config.get("known_kitchens", {}).get(run.kitchen)
        if expected and slug != expected:
            run.add("Public kitchen", "FAIL", f"Expected kitchen {expected}, opened {slug}", screenshot(page, run, "public-wrong-kitchen"))
            return
        page.get_by_role("heading", name=config.get("kitchen_heading", "Customize your kitchen")).wait_for(state="visible", timeout=15000)
        plan = page.locator(config["plan_image"]).first
        plan.wait_for(state="visible", timeout=15000)
        plan_alt = plan.get_attribute("alt") or ""
        slug_number = re.search(r"\d+", slug)
        if slug_number and not exact_reference(plan_alt, slug_number.group(0)):
            run.add("Plan reference", "MISMATCH", f"Kitchen {slug} but plan label {plan_alt}", screenshot(page, run, "wrong-plan"))
            return
        run.add("Plan reference", "MATCH", f"Kitchen {slug}; plan label {plan_alt}")
        run.plan_url = urljoin(page.url, plan.get_attribute("src") or "")
        run.add("Public kitchen", "PASS", f"Contract {run.kitchen} opened kitchen {slug}", screenshot(page, run, "public-kitchen"))
        run.reference = ""
        check_images(page, config.get("images", ""), run, "Public")
        if interactive:
            page.route("**/*", lambda route: route.abort() if route.request.method not in ("GET", "HEAD", "OPTIONS") else route.continue_())
            print("Choose options in the visible Fragmento page, then press Enter here to verify them.", flush=True)
            print("SELECTION_READY", flush=True)
            input()
        selected_ok = test_interactive_selection(page, config, run) if interactive else test_public_selection(page, config, run)
        if selected_ok:
            checkout_check(page, config, run, interactive=interactive)
    except PlaywrightTimeout:
        run.add("Public kitchen", "NOT_FOUND", "Contract number did not open a kitchen page, or public lookup timed out", screenshot(page, run, "public-not-found"))
    except PlaywrightError as exc:
        run.add("Public kitchen", "FAIL", f"Public kitchen page failed: {type(exc).__name__}")


def component_count(page, config: dict) -> int | None:
    name = label(config, "component_count", "Components")
    match = re.search(r"\b(\d+)\s+" + re.escape(name) + r"\b", page.locator("body").inner_text(), re.I)
    return int(match.group(1)) if match else None


def parse_money(text: str) -> Decimal | None:
    match = re.search(r"(\d[\d., \u00a0]*[.,]\d{2})\s*€", text)
    if not match:
        return None
    value = match.group(1).replace(" ", "").replace("\u00a0", "")
    try:
        return Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def summary_panel(page, config: dict):
    return page.get_by_role("heading", name=label(config, "summary_heading", "Your selection")).locator("xpath=../../..")


def summary_totals(page, config: dict) -> dict[str, Decimal | None]:
    panel = summary_panel(page, config)
    result = {}
    for key, default in (("subtotal", "Price"), ("vat", "VAT (19%)"), ("total_price", "Total price")):
        row = panel.get_by_text(label(config, key, default), exact=True).first.locator("xpath=..")
        result[key] = parse_money(row.inner_text())
    return result


def test_public_selection(page, config: dict, run: Run) -> bool:
    """Exercise available Add options while blocking write HTTP methods."""
    try:
        options = []
        add_text = label(config, "add", "Add +")
        remove_text = label(config, "remove", "Remove -")
        for i in range(page.get_by_role("button").filter(has_text=add_text).count()):
            button = page.get_by_role("button").filter(has_text=add_text).nth(i)
            if button.is_enabled():
                options.append(button.inner_text().strip().splitlines()[0])
        if not options:
            run.add("Component selection", "FAIL", "No enabled Add option was visible", screenshot(page, run, "selection-no-option"))
            return False
        original_image = screenshot(page, run, "selection-before")
        run.add("Original selection", "PASS", "State before Add options", original_image)
        page.route("**/*", lambda route: route.abort() if route.request.method not in ("GET", "HEAD", "OPTIONS") else route.continue_())
        for index, name in enumerate(options, start=1):
            button = page.get_by_role("button").filter(has_text=name).filter(has_text=add_text).first
            if not button.count() or not button.is_enabled():
                run.add(f"Select {name}", "INCOMPLETE", "Option was unavailable after earlier choices")
                continue
            listed_price = parse_money(button.inner_text())
            document_labels = button.locator('[class*="DocChip"]').all_inner_texts()
            if document_labels:
                run.expected_documents[name] = [item.strip() for item in document_labels if item.strip()]
            before_count = component_count(page, config)
            before_totals = summary_totals(page, config)
            button.click(timeout=10000)
            selected = page.get_by_role("button").filter(has_text=name).filter(has_text=remove_text).first
            selected.wait_for(state="visible", timeout=10000)
            after_count = component_count(page, config)
            after_totals = summary_totals(page, config)
            delta = None if before_totals["total_price"] is None or after_totals["total_price"] is None else after_totals["total_price"] - before_totals["total_price"]
            price_ok = listed_price is not None and delta == listed_price
            count_ok = before_count is not None and after_count is not None and after_count in (before_count, before_count + 1)
            status = "PASS" if price_ok and count_ok else "FAIL"
            run.add(f"Select {name}", status,
                    f"Listed price {listed_price}; total change {delta}; components {before_count} to {after_count}; write requests blocked",
                    screenshot(page, run, f"selection-{index}-after"))
            run.selection[f"option:{name}:price"] = str(listed_price) if listed_price is not None else ""
            run.selection[f"option:{name}:quantity"] = "1"
            for document_label in run.expected_documents.get(name, []):
                run.add(f"Selected document {name}", "PASS", f"Public option advertises {document_label}")
            if status != "PASS":
                return False
        final_totals = summary_totals(page, config)
        run.selection["contract_number"] = run.kitchen
        run.selection["total_price"] = str(final_totals["total_price"]) if final_totals["total_price"] is not None else ""
        return True
    except PlaywrightTimeout:
        run.add("Component selection", "FAIL", "Selection did not visibly change to Remove", screenshot(page, run, "selection-failed"))
    except PlaywrightError as exc:
        run.add("Component selection", "FAIL", f"Selection check failed: {type(exc).__name__}")
    return False


def test_interactive_selection(page, config: dict, run: Run) -> bool:
    """Test every visible choice, restoring the customer's original selection."""
    try:
        add_text = label(config, "add", "Add +")
        remove_text = label(config, "remove", "Remove -")
        option_names = []
        for button in page.get_by_role("button").all():
            content = button.inner_text().strip()
            if button.is_enabled() and (add_text in content or remove_text in content):
                name = content.splitlines()[0]
                if name not in option_names:
                    option_names.append(name)
        if not option_names:
            run.add("Component selection", "FAIL", "No selectable options were visible")
            return False
        run.add("Original selection", "PASS", "Customer choices before verification", screenshot(page, run, "selection-before"))
        all_good = True
        for index, name in enumerate(option_names, start=1):
            button = page.get_by_role("button").filter(has_text=name).filter(has_text=remove_text).first
            originally_selected = button.count() > 0
            if not originally_selected:
                button = page.get_by_role("button").filter(has_text=name).filter(has_text=add_text).first
            listed_price = parse_money(button.inner_text())
            before_count = component_count(page, config)
            before_total = summary_totals(page, config)["total_price"]
            button.click(timeout=10000)
            opposite = add_text if originally_selected else remove_text
            changed = page.get_by_role("button").filter(has_text=name).filter(has_text=opposite).first
            changed.wait_for(state="visible", timeout=10000)
            changed_count = component_count(page, config)
            changed_total = summary_totals(page, config)["total_price"]
            delta = None if before_total is None or changed_total is None else changed_total - before_total
            expected_delta = -listed_price if originally_selected and listed_price is not None else listed_price
            count_ok = before_count is not None and changed_count is not None and abs(before_count - changed_count) <= 1
            changed.click(timeout=10000)
            restored = page.get_by_role("button").filter(has_text=name).filter(has_text=remove_text if originally_selected else add_text).first
            restored.wait_for(state="visible", timeout=10000)
            after_total = summary_totals(page, config)["total_price"]
            good = delta is not None and expected_delta is not None and delta == expected_delta and after_total == before_total and count_ok
            run.add(f"Select {name}", "PASS" if good else "FAIL",
                    f"Originally {'selected' if originally_selected else 'unselected'}; price {listed_price}; state changed and restored; total change {delta}",
                    screenshot(page, run, f"selection-{index}-after"))
            all_good &= good
            if originally_selected:
                run.selection[f"option:{name}:price"] = str(listed_price) if listed_price is not None else ""
                run.selection[f"option:{name}:quantity"] = "1"
                document_labels = restored.locator('[class*="DocChip"]').all_inner_texts()
                if document_labels:
                    run.expected_documents[name] = [item.strip() for item in document_labels if item.strip()]
                for document_label in run.expected_documents.get(name, []):
                    run.add(f"Selected document {name}", "PASS", f"Public option advertises {document_label}")
        run.selection["contract_number"] = run.kitchen
        total = summary_totals(page, config)["total_price"]
        run.selection["total_price"] = str(total) if total is not None else ""
        return all_good
    except (PlaywrightError, PlaywrightTimeout) as exc:
        run.add("Component selection", "FAIL", f"Choice verification failed: {type(exc).__name__}")
        return False


CONTACT_FIELDS = {
    "first_name": ("FRAGMENTO_FIRST_NAME", "#firstName"),
    "last_name": ("FRAGMENTO_LAST_NAME", "#lastName"),
    "contact_email": ("FRAGMENTO_CONTACT_EMAIL", "#email"),
    "phone": ("FRAGMENTO_PHONE", "#phone"),
    "address_line_1": ("FRAGMENTO_ADDRESS1", "#address1"),
    "country": ("FRAGMENTO_COUNTRY", "#country"),
    "city": ("FRAGMENTO_CITY", "#city"),
    "postal_code": ("FRAGMENTO_POSTAL_CODE", "#postalCode"),
    "delivery_week": ("FRAGMENTO_DELIVERY_WEEK", "#preferredDeliveryDate"),
}


def masked_checkout_screenshot(page, run: Run) -> str:
    folder = REPORTS / f"{run.started:%Y%m%d-%H%M%S-%f}-{run.kitchen}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "checkout-masked.png"
    page.screenshot(path=str(target), full_page=True,
                    mask=[page.locator("input:not([type=hidden]), textarea, select")])
    return str(target.relative_to(REPORTS)).replace("\\", "/")


def lock_paid_order_button(page, button_text: str) -> None:
    """Keep the paid-order control disabled during a manual inspection."""
    page.evaluate("""text => {
      const lock = () => document.querySelectorAll('button').forEach(button => {
        if (button.textContent && button.textContent.includes(text) && !button.disabled) {
          button.disabled = true;
          button.title = 'Disabled during the QA check';
        }
      });
      lock();
      new MutationObserver(lock).observe(document.body, {subtree: true, childList: true, attributes: true});
    }""", button_text)


def prefill_manual_contact(page) -> None:
    """Offer saved local details in the real form without submitting them."""
    for key in ("first_name", "last_name", "contact_email", "phone", "address_line_1"):
        value = os.getenv(CONTACT_FIELDS[key][0], "").strip()
        if value:
            page.locator(CONTACT_FIELDS[key][1]).fill(value)
    for key, selector in (("FRAGMENTO_ADDRESS2", "#address2"), ("FRAGMENTO_NOTES", "#notes")):
        value = os.getenv(key, "").strip()
        if value:
            page.locator(selector).fill(value)
    for key in ("country", "city", "postal_code"):
        value = os.getenv(CONTACT_FIELDS[key][0], "").strip()
        if value:
            try:
                page.locator(CONTACT_FIELDS[key][1]).select_option(label=value, timeout=3000)
            except PlaywrightError:
                break
    week = os.getenv("FRAGMENTO_DELIVERY_WEEK", "").strip()
    if week:
        try:
            page.locator("#preferredDeliveryDate").click()
            page.get_by_role("option", name=week, exact=True).click(timeout=3000)
        except PlaywrightError:
            pass


def checkout_check(page, config: dict, run: Run, interactive: bool = False) -> None:
    """Capture customer summary and fill local details; never activate submit."""
    try:
        page.get_by_role("button", name=label(config, "continue_to_order", "Continue to order")).click(timeout=10000)
        page.locator("#firstName").wait_for(state="visible", timeout=15000)
        if interactive:
            lock_paid_order_button(page, label(config, "paid_order_button", "Complete paid order"))
        displayed_contract = page.locator("#contractNumber").input_value()
        match = displayed_contract == run.kitchen
        run.summary["contract_number"] = displayed_contract
        run.add("Order form contract", "MATCH" if match else "MISMATCH",
                f"Expected {run.kitchen}; displayed {displayed_contract}", screenshot(page, run, "order-form"))
        if not match:
            return
        totals = summary_totals(page, config)
        for key in ("subtotal", "vat", "total_price"):
            amount = totals[key]
            if amount is not None:
                run.summary[key] = str(amount)
            run.add(f"Order summary {key}", "PASS" if amount is not None else "FAIL", str(amount) if amount is not None else "Missing value")
        if all(totals[key] is not None for key in ("subtotal", "vat", "total_price")):
            good = totals["subtotal"] + totals["vat"] == totals["total_price"]
            run.add("Order summary arithmetic", "PASS" if good else "FAIL", "Subtotal plus VAT equals total" if good else "Subtotal plus VAT differs from total")
        for key, expected_price in list(run.selection.items()):
            if not key.startswith("option:") or not key.endswith(":price"):
                continue
            name = key[len("option:"):-len(":price")]
            item = summary_panel(page, config).get_by_text(name, exact=True)
            if item.count() != 1:
                run.add(f"Order summary option {name}", "FAIL", f"Expected one selected row; found {item.count()}")
                continue
            row = item.first.locator("xpath=../..")
            actual_price = parse_money(row.inner_text())
            actual_quantity = extract(r"[×x]\s*(\d+)", row.inner_text()) or "1"
            run.summary[f"option:{name}:price"] = str(actual_price) if actual_price is not None else ""
            run.summary[f"option:{name}:quantity"] = actual_quantity
            good = actual_price is not None and str(actual_price) == expected_price and actual_quantity == "1"
            run.add(f"Order summary option {name}", "MATCH" if good else "MISMATCH",
                    f"Quantity {actual_quantity}; selected price {expected_price}; summary price {actual_price}")
        if interactive:
            prefill_manual_contact(page)
            print("Fill the visible Fragmento form, then press Enter here to verify it.", flush=True)
            print("FORM_READY", flush=True)
            input()
            inspect_manual_contact(page, run)
            run.add("Checkout submission", "NOT_RUN", "Paid-order action was disabled; no order or payment created")
            return
        contact = {key: os.getenv(env, "").strip() for key, (env, _) in CONTACT_FIELDS.items()}
        missing = [CONTACT_FIELDS[key][0] for key, value in contact.items() if not value]
        if missing:
            run.add("Delivery/contact", "UNCONFIGURED", "Missing local variables: " + ", ".join(missing))
            run.add("Checkout submission", "NOT_RUN", "Stopped before Complete paid order; no order or payment created")
            return
        for key in ("first_name", "last_name", "contact_email", "phone", "address_line_1"):
            page.locator(CONTACT_FIELDS[key][1]).fill(contact[key])
        if os.getenv("FRAGMENTO_ADDRESS2", ""):
            page.locator("#address2").fill(os.getenv("FRAGMENTO_ADDRESS2", ""))
        if os.getenv("FRAGMENTO_NOTES", ""):
            page.locator("#notes").fill(os.getenv("FRAGMENTO_NOTES", ""))
        for key in ("country", "city", "postal_code"):
            page.locator(CONTACT_FIELDS[key][1]).select_option(label=contact[key], timeout=10000)
        page.locator("#preferredDeliveryDate").click()
        page.get_by_role("option", name=contact["delivery_week"], exact=True).click()
        for key, (_, selector) in CONTACT_FIELDS.items():
            if key in ("country", "city", "postal_code"):
                actual = page.locator(selector).locator("option:checked").inner_text().strip()
            elif key == "delivery_week":
                actual = page.locator(selector).inner_text().strip()
            else:
                actual = page.locator(selector).input_value().strip()
            run.summary[f"delivery:{key}"] = actual
            run.add(f"Delivery/contact {key}", "MATCH" if normalized(actual) == normalized(contact[key]) else "MISMATCH",
                    "Displayed value matches local configuration" if normalized(actual) == normalized(contact[key]) else "Displayed value differs from local configuration")
        run.add("Delivery/contact screenshot", "PASS", "Personal fields masked", masked_checkout_screenshot(page, run))
        run.add("Checkout submission", "NOT_RUN", "Stopped before Complete paid order; no order or payment created")
    except PlaywrightTimeout:
        evidence = masked_checkout_screenshot(page, run) if page.locator("#firstName").count() else screenshot(page, run, "order-form-error")
        run.add("Order form", "FAIL", "Order form or configured delivery choice did not load", evidence)
    except PlaywrightError as exc:
        run.add("Order form", "FAIL", f"Order form check failed: {type(exc).__name__}")


def inspect_manual_contact(page, run: Run) -> None:
    for key, (_, selector) in CONTACT_FIELDS.items():
        try:
            field = page.locator(selector)
            if key in ("country", "city", "postal_code"):
                actual = field.locator("option:checked").inner_text().strip()
                valid = bool(field.input_value())
            elif key == "delivery_week":
                actual = field.inner_text().strip()
                valid = bool(page.locator('input[name="preferred-delivery-date"]').input_value())
            else:
                actual = field.input_value().strip()
                valid = bool(actual) and field.evaluate("el => el.checkValidity()")
            run.summary[f"delivery:{key}"] = actual if valid else ""
            run.add(f"Delivery/contact {key}", "PASS" if valid else "FAIL",
                    "Filled with a valid page choice" if valid else "Required field is empty or invalid")
        except PlaywrightError:
            run.add(f"Delivery/contact {key}", "FAIL", "Field could not be read")
    run.add("Delivery/contact screenshot", "PASS", "Personal fields masked", masked_checkout_screenshot(page, run))


def outlook_client():
    client_id = os.getenv("FRAGMENTO_MS_CLIENT_ID", "")
    if not client_id:
        return None, None
    cache = msal.SerializableTokenCache()
    if MAIL_CACHE.is_file():
        cache.deserialize(MAIL_CACHE.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(client_id, authority="https://login.microsoftonline.com/consumers", token_cache=cache)
    return app, cache


def save_mail_cache(cache) -> None:
    if cache.has_state_changed:
        MAIL_CACHE.parent.mkdir(exist_ok=True)
        MAIL_CACHE.write_text(cache.serialize(), encoding="utf-8")


def setup_mail() -> int:
    app, cache = outlook_client()
    if not app:
        print("Set FRAGMENTO_MS_CLIENT_ID in .env first")
        return 2
    flow = app.initiate_device_flow(scopes=["Mail.Read", "User.Read"])
    if "user_code" not in flow:
        print("Could not start Microsoft authorization")
        return 2
    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print("Microsoft authorization failed")
        return 2
    save_mail_cache(cache)
    print("Read-only mailbox access saved locally in .auth")
    return 0


def graph_get(url: str, token: str, params: dict | None = None) -> dict:
    response = requests.get(url, headers={
        "Authorization": f"Bearer {token}",
        "Prefer": 'outlook.body-content-type="text"',
    }, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def check_email_attachments(config: dict, run: Run, message: dict, token: str) -> None:
    if not message.get("hasAttachments"):
        run.add("Email attachments", "PASS", "Matching message has no file attachments")
        attachments = []
    else:
        data = graph_get(f"https://graph.microsoft.com/v1.0/me/messages/{message['id']}/attachments", token)
        attachments = data.get("value", [])
        next_page = data.get("@odata.nextLink")
        while next_page:
            data = graph_get(next_page, token)
            attachments.extend(data.get("value", []))
            next_page = data.get("@odata.nextLink")
        run.add("Email attachments", "PASS", f"{len(attachments)} attachment(s) listed in exact matching message")

    rules = config.get("attachment_rules", {})
    for option, documents in run.expected_documents.items():
        for document in documents:
            pattern = rules.get(document, "")
            check_name = f"Email document {option}: {document}"
            if not configured(pattern):
                run.add(check_name, "UNCONFIGURED", "Set a filename pattern from a real confirmation email")
                continue
            found = any(re.search(pattern, item.get("name", ""), re.I) for item in attachments)
            run.add(check_name, "PASS" if found else "FAIL",
                    "Matching attachment filename found" if found else "Expected attachment filename absent")

    pattern = config.get("plan_attachment_pattern", "")
    if not configured(pattern):
        run.add("Email plan file", "UNCONFIGURED", "Set plan attachment filename pattern from a real email")
        return
    plans = [item for item in attachments if re.search(pattern, item.get("name", ""), re.I)]
    if len(plans) != 1:
        run.add("Email plan file", "FAIL" if not plans else "AMBIGUOUS",
                f"Expected one plan attachment; found {len(plans)}")
        return
    attachment = plans[0]
    if not run.plan_url:
        run.add("Email plan file", "INCOMPLETE", "Public plan URL was unavailable")
        return
    public_type = mimetypes.guess_type(urlparse(run.plan_url).path)[0]
    attached_type = attachment.get("contentType") or mimetypes.guess_type(attachment.get("name", ""))[0]
    if public_type != attached_type:
        run.add("Email plan file", "INCOMPLETE", "Plan formats differ; visual comparison needs an approved reference or renderer")
        return
    content = attachment.get("contentBytes")
    if not content:
        content = graph_get(f"https://graph.microsoft.com/v1.0/me/messages/{message['id']}/attachments/{attachment['id']}", token).get("contentBytes")
    if not content:
        run.add("Email plan file", "INCOMPLETE", "Attachment bytes were unavailable from Outlook")
        return
    public_response = requests.get(run.plan_url, timeout=30)
    public_response.raise_for_status()
    same = hashlib.sha256(base64.b64decode(content)).digest() == hashlib.sha256(public_response.content).digest()
    run.add("Email plan file", "MATCH" if same else "MISMATCH",
            "Plan files have identical bytes" if same else "Plan file bytes differ; visual equivalence is unverified")


def find_outlook_email(config: dict, run: Run) -> None:
    expected_address = os.getenv("FRAGMENTO_MAIL_ADDRESS", "")
    try:
        app, cache = outlook_client()
        if not app or not expected_address:
            run.add("Email", "UNCONFIGURED", "Set Microsoft client ID and expected Outlook address in .env")
            return
        accounts = app.get_accounts()
        if len(accounts) != 1:
            run.add("Email", "UNCONFIGURED", "Run --setup-mail for exactly one Outlook account")
            return
        token_result = app.acquire_token_silent(["Mail.Read", "User.Read"], account=accounts[0])
        save_mail_cache(cache)
        if not token_result or "access_token" not in token_result:
            run.add("Email", "LOGIN_FAILURE", "Outlook token expired; run --setup-mail")
            return
        token = token_result["access_token"]
        profile = graph_get("https://graph.microsoft.com/v1.0/me", token, {"$select": "mail,userPrincipalName"})
        addresses = {normalized(profile.get("mail") or ""), normalized(profile.get("userPrincipalName") or "")}
        if normalized(expected_address) not in addresses:
            run.add("Email", "LOGIN_FAILURE", "Authorized mailbox differs from FRAGMENTO_MAIL_ADDRESS")
            return
        days = max(1, min(int(config.get("search_window_days", 90)), 3650))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
        url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
        params = {"$select": "id,subject,body,receivedDateTime,hasAttachments", "$filter": f"receivedDateTime ge {since}", "$top": 100}
        matches = []
        pages = 0
        while url and pages < 20:
            data = graph_get(url, token, params)
            pages += 1
            params = None
            for message in data.get("value", []):
                body = message.get("body", {}).get("content") or ""
                searchable = f"{message.get('subject', '')}\n{body}"
                if exact_reference(searchable, run.kitchen) or (run.reference and exact_reference(searchable, run.reference)):
                    matches.append((message, body))
            url = data.get("@odata.nextLink", "")
        if url:
            run.add("Email", "INCOMPLETE", "Inbox search reached the page limit; no reliable uniqueness result")
            return
        if not matches:
            run.add("Email", "EMAIL_NOT_FOUND", f"No exact message for kitchen {run.kitchen} or verified reference in last {days} days")
            return
        if len(matches) != 1:
            run.add("Email", "AMBIGUOUS", f"{len(matches)} matching messages")
            return
        message, body = matches[0]
        run.add("Email", "PASS", f"Matched message received {message.get('receivedDateTime', 'unknown')}")
        if not config.get("field_patterns"):
            run.add("Email fields", "UNCONFIGURED", "Set field patterns from a real matching confirmation email")
        for name, pattern in config.get("field_patterns", {}).items():
            if re.search(r"card|cvv|cvc|payment|password|iban|token", name, re.I):
                run.add(f"Email {name}", "UNCONFIGURED", "Payment and credential fields are excluded")
                continue
            if not configured(pattern):
                run.add(f"Email {name}", "UNCONFIGURED", "Field pattern is missing")
                continue
            value = extract(pattern, body)
            if value:
                run.email[name] = value
            run.add(f"Email {name}", "PASS" if value else "FAIL", "Value extracted" if value else "Field not found in matching email")
        check_email_attachments(config, run, message, token)
    except (requests.RequestException, ValueError, OSError, KeyError) as exc:
        run.add("Email", "FAIL", f"Outlook inbox read failed: {type(exc).__name__}")


def compare(run: Run) -> None:
    for name, expected in run.selection.items():
        actual = run.summary.get(name, "")
        status = "MATCH" if actual and comparable(name, expected) == comparable(name, actual) else "MISMATCH" if actual else "INCOMPLETE"
        run.add(f"Selection vs order summary {name}", status,
                f"Selection: {expected}; summary: {actual or 'missing'}")
    if any(check.name == "Email" and check.status == "PASS" for check in run.checks):
        for name, email_value in run.email.items():
            actual = run.summary.get(name, "")
            status = "MATCH" if actual and comparable(name, actual) == comparable(name, email_value) else "MISMATCH" if actual else "INCOMPLETE"
            detail = "Values match" if status == "MATCH" else "Summary value differs from email" if status == "MISMATCH" else "Value missing from order summary"
            if not name.startswith("delivery:") and status == "MISMATCH":
                detail += f"; summary: {actual}; email: {email_value}"
            run.add(f"Email vs order summary {name}", status,
                    detail)


def write_report(run: Run) -> Path:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{run.started:%Y%m%d-%H%M%S-%f}-{run.kitchen}.html"
    bad = {"FAIL", "MISMATCH", "NOT_FOUND", "AMBIGUOUS", "LOGIN_FAILURE"}
    incomplete = {"UNCONFIGURED", "INCOMPLETE", "NOT_RUN", "EMAIL_NOT_FOUND"}
    overall = "FAIL" if any(c.status in bad for c in run.checks) else "INCOMPLETE" if any(c.status in incomplete for c in run.checks) else "PASS"
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for check in run.checks:
        evidence = f'<a href="{esc(check.screenshot)}"><img src="{esc(check.screenshot)}" alt="{esc(check.name)} screenshot" width="220"></a>' if check.screenshot else ""
        rows.append(f"<tr><td>{esc(check.name)}</td><td class='{esc(check.status)}'>{esc(check.status)}</td><td>{esc(check.detail)}</td><td>{evidence}</td></tr>")
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Kitchen {esc(run.kitchen)} QA</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}th,td{{padding:.6rem;border:1px solid #bbb;text-align:left;vertical-align:top}}.PASS,.MATCH{{color:#087333}}.FAIL,.MISMATCH,.NOT_FOUND,.AMBIGUOUS,.LOGIN_FAILURE{{color:#b42318;font-weight:bold}}.UNCONFIGURED,.INCOMPLETE,.NOT_RUN,.EMAIL_NOT_FOUND{{color:#955600}}</style>
<h1>Kitchen {esc(run.kitchen)} — {overall}</h1><p>Started: {esc(run.started.isoformat(timespec='seconds'))}</p>
<table><thead><tr><th>Check</th><th>Status</th><th>Details</th><th>Evidence</th></tr></thead><tbody>{''.join(rows)}</tbody></table></html>"""
    path.write_text(document, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Fragmento kitchen QA checker")
    parser.add_argument("--kitchen", help="Kitchen number to check")
    parser.add_argument("--config", type=Path, default=ROOT / "workflow.json")
    parser.add_argument("--setup-mail", action="store_true", help="Authorize read-only Outlook inbox access")
    parser.add_argument("--interactive", action="store_true", help="Open the public form visibly for manual contact entry")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if args.setup_mail:
        return setup_mail()
    try:
        config = read_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    kitchen = (args.kitchen if args.kitchen is not None else input("Enter kitchen number: ")).strip()
    run = Run(kitchen or "invalid")
    pattern = config.get("kitchen_number_pattern", r"^[0-9]+$")
    try:
        valid = bool(re.fullmatch(pattern, kitchen))
    except re.error:
        print("Invalid kitchen_number_pattern in workflow.json", file=sys.stderr)
        return 2
    if not valid:
        run.add("Kitchen number", "FAIL", "Invalid kitchen number format")
        path = write_report(run)
        print(f"Invalid kitchen number. Report: {path}")
        return 1
    run.add("Kitchen number", "PASS", kitchen)
    public = config.get("public", config.get("selection", {}))
    if check_url(public.get("url", "")):
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not args.interactive)
                context = browser.new_context()
                page = context.new_page()
                public_contract_check(page, public, run, interactive=args.interactive)
                context.close()
                browser.close()
        except (PlaywrightError, OSError, ValueError) as exc:
            run.add("Browser", "FAIL", f"Browser could not start: {type(exc).__name__}")
    else:
        run.add("Public kitchen", "UNCONFIGURED", "Set the public Fragmento URL in workflow.json")
    find_outlook_email(config.get("email", {}), run)
    if not any(check.name == "Email" and check.status == "PASS" for check in run.checks):
        for name in ("Email fields", "Email attachments", "Email plan file"):
            run.add(name, "NOT_RUN", "Requires an authorized inbox and an exact matching confirmation email")
    compare(run)
    path = write_report(run)
    overall = "FAIL" if any(c.status in {"FAIL", "MISMATCH", "NOT_FOUND", "AMBIGUOUS", "LOGIN_FAILURE"} for c in run.checks) else "INCOMPLETE" if any(c.status in {"UNCONFIGURED", "INCOMPLETE", "NOT_RUN", "EMAIL_NOT_FOUND"} for c in run.checks) else "PASS"
    print(f"Kitchen {kitchen}: {overall}")
    for check in run.checks:
        print(f"- {check.name}: {check.status}" + (f" — {check.detail}" if check.detail else ""))
    print(f"Report: {path}")
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
