"""Public Fragmento visual QA with verified Stripe test-mode boundary."""

from __future__ import annotations

import argparse
import ast
import html
import json
import os
import re
import secrets
import sys
import unicodedata
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from visual_qa import compare_plan_images


ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
MAIL_PROFILE = ROOT / ".auth" / "outlook-browser"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    screenshot: str = ""
    evidence: list[str] = field(default_factory=list)


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
    final_plan: str = ""
    confirmation_reference: str = ""
    inventory: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", screenshot: str = "", evidence: list[str] | None = None) -> None:
        self.checks.append(Check(name, status, detail, screenshot, evidence or []))


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


def plan_screenshot(page, config: dict, run: Run, name: str) -> str:
    folder = REPORTS / f"{run.started:%Y%m%d-%H%M%S-%f}-{run.kitchen}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.png"
    page.locator(config["plan_image"]).first.screenshot(path=str(target))
    return str(target.relative_to(REPORTS)).replace("\\", "/")


def evidence_path(relative: str) -> Path:
    return REPORTS / relative


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


def public_contract_check(page, config: dict, run: Run, interactive: bool = False,
                          visual: dict | None = None, pre_submit_only: bool = False) -> None:
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
        selected_ok = test_interactive_selection(page, config, run) if interactive else visual_all_selection(page, config, visual or {}, run)
        if selected_ok:
            checkout_check(page, config, run, interactive=interactive, pre_submit_only=pre_submit_only)
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


def element_rows(page, config: dict) -> list[str]:
    """Read all summary rows, including collapsed and repeated fixed components."""
    rows = summary_panel(page, config).locator('[class*="summaryRow__"]')
    names = []
    for row in rows.all():
        name = row.locator('[class*="summaryMeta__"] strong').first.inner_text().strip()
        if name:
            names.append(name)
    return names


def element_details(page, config: dict) -> list[dict[str, str]]:
    details = []
    for row in summary_panel(page, config).locator('[class*="summaryRow__"]').all():
        name = row.locator('[class*="summaryMeta__"] strong').first.inner_text().strip()
        article_locator = row.locator('[class*="itemCode__"]')
        price_locator = row.locator('[class*="summaryPrice__"]')
        if name:
            details.append({"name": name, "article": article_locator.first.inner_text().strip() if article_locator.count() else "",
                            "price": price_locator.first.inner_text().strip() if price_locator.count() else "",
                            "quantity": "1"})
    return details


def visual_all_selection(page, config: dict, visual: dict, run: Run) -> bool:
    """Select every enabled Add card and compare the rendered plan and complete list."""
    try:
        add_text = label(config, "add", "Add +")
        remove_text = label(config, "remove", "Remove -")
        run.inventory = element_rows(page, config)
        if not run.inventory:
            run.add("Element inventory", "FAIL", "No kitchen elements were listed")
            return False
        initial_plan = plan_screenshot(page, config, run, "plan-before")
        fixed_reference = visual.get("fixed_plan_reference", "")
        reference_status = "REVIEW"
        if fixed_reference:
            reference_path = (ROOT / fixed_reference).resolve()
            if reference_path.is_file():
                fixed_diff = compare_plan_images(reference_path, evidence_path(initial_plan))["changed_fraction"]
                reference_status = "PASS" if fixed_diff <= float(visual.get("reference_tolerance", 0.02)) else "FAIL"
            else:
                reference_status = "INCOMPLETE"
        for index, detail in enumerate(element_details(page, config), 1):
            run.add(f"Element {index}: {detail['name']}", reference_status,
                    f"Quantity {detail['quantity']}; {detail['article']}; displayed price/status {detail['price']}; "
                    + ("initial plan compared with approved reference" if fixed_reference else "visual placement needs an approved reference"),
                    initial_plan)
        choices = []
        for button in page.get_by_role("button").all():
            if not button.is_enabled():
                continue
            content = button.inner_text().strip()
            if add_text in content or remove_text in content:
                name = content.splitlines()[0]
                if name not in [item[0] for item in choices]:
                    choices.append((name, remove_text in content))
        if not choices:
            run.add("Selectable elements", "INCOMPLETE", "No enabled Add options found for this kitchen")
        run.add("Original selection", "PASS", f"{len(run.inventory)} listed elements", screenshot(page, run, "selection-before"),
                [initial_plan])
        page.route("**/*", lambda route: route.abort() if route.request.method not in ("GET", "HEAD", "OPTIONS") else route.continue_())
        passed = reference_status not in {"FAIL", "INCOMPLETE"}
        excluded_names: set[str] = set()
        for index, (name, was_selected) in enumerate(choices, 1):
            button = page.get_by_role("button").filter(has_text=name).filter(has_text=remove_text if was_selected else add_text).first
            if not button.count() or not button.is_enabled():
                run.add(f"Select {name}", "INCOMPLETE", "Option became unavailable after preceding selections")
                passed = False
                continue
            before_list = element_rows(page, config)
            before_count = component_count(page, config)
            before_total = summary_totals(page, config)["total_price"]
            price = parse_money(button.inner_text())
            before_page = screenshot(page, run, f"element-{index}-before")
            before_plan = plan_screenshot(page, config, run, f"element-{index}-plan-before")
            doc_labels = button.locator('[class*="DocChip"]').all_inner_texts()
            fixed_conflict = config.get("incompatible_when_fixed", {}).get(name, "")
            conflicting_fixed = bool(fixed_conflict and any(
                fixed_conflict in fixed.inner_text() and "Fixed" in fixed.inner_text()
                for fixed in page.get_by_role("button").all() if not fixed.is_enabled()))
            button.click(timeout=10000)
            selected = page.get_by_role("button").filter(has_text=name).filter(has_text=add_text if was_selected else remove_text).first
            selected.wait_for(state="visible", timeout=10000)
            after_list = element_rows(page, config)
            after_count = component_count(page, config)
            after_total = summary_totals(page, config)["total_price"]
            after_page = screenshot(page, run, f"element-{index}-after")
            after_plan = plan_screenshot(page, config, run, f"element-{index}-plan-after")
            before_items, after_items = Counter(before_list), Counter(after_list)
            retained = all(after_items[item] >= quantity for item, quantity in before_items.items())
            unique = after_items[name] == before_items[name] + (-1 if was_selected else 1)
            price_good = price is not None and before_total is not None and after_total - before_total == (-price if was_selected else price)
            count_good = before_count is not None and after_count == before_count + (-1 if was_selected else 1)
            if was_selected:
                retained = all(after_items[item] >= quantity for item, quantity in before_items.items() if item != name)
            good = retained and unique and price_good and count_good
            passed &= good
            if (was_selected and not conflicting_fixed) or (conflicting_fixed and not was_selected):
                selected.click(timeout=10000)
                page.get_by_role("button").filter(has_text=name).filter(
                    has_text=remove_text if was_selected else add_text).first.wait_for(state="visible", timeout=10000)
                restored = element_rows(page, config) == before_list and summary_totals(page, config)["total_price"] == before_total
                good &= restored
            run.add(f"Select {name}", "PASS" if good else "FAIL",
                    f"Before: {before_list}; after: {after_list}; count {before_count}->{after_count}; price {before_total}->{after_total}",
                    before_page, [before_plan, after_page, after_plan])
            if conflicting_fixed:
                excluded_names.add(name)
                run.add(f"Excluded incompatible option {name}", "PASS" if good else "FAIL",
                        f"Tested and restored to unselected because fixed service {fixed_conflict} is present")
            else:
                run.selection[f"option:{name}:price"] = str(price) if price is not None else ""
                run.selection[f"option:{name}:quantity"] = "1"
                run.expected_documents[name] = [value.strip() for value in doc_labels if value.strip()]
            result = compare_plan_images(evidence_path(before_plan), evidence_path(after_plan))
            changed = result["changed_fraction"]
            reference = visual.get("plan_references", {}).get(name, "")
            if reference:
                reference_path = (ROOT / reference).resolve()
                if not reference_path.is_file():
                    run.add(f"Visual {name}", "INCOMPLETE", "Configured reference image is missing", after_plan)
                else:
                    match = compare_plan_images(reference_path, evidence_path(after_plan))["changed_fraction"] <= float(visual.get("reference_tolerance", 0.02))
                    run.add(f"Visual {name}", "PASS" if match else "FAIL", "Rendered plan matches approved reference" if match else "Rendered plan differs from approved reference", after_plan, [before_plan])
                    passed &= match
            else:
                reason = "Visible pixels changed; correct object and placement still need a reference" if changed >= float(visual.get("minimum_changed_fraction", 0.003)) else "No clear rendered plan change for this option"
                run.add(f"Visual {name}", "REVIEW", f"{reason}; changed fraction {changed:.4f}", after_plan, [before_plan])
        final_list = element_rows(page, config)
        expected = Counter(run.inventory)
        for name, was_selected in choices:
            if name in excluded_names and was_selected:
                expected[name] -= 1
            elif not was_selected and name not in excluded_names:
                expected[name] += 1
        complete = Counter(final_list) == expected
        run.add("Final element inventory", "PASS" if complete else "FAIL",
                f"Expected {list(expected.elements())}; found {final_list}")
        passed &= complete
        run.inventory = final_list
        run.final_plan = plan_screenshot(page, config, run, "final-plan")
        run.add("Final sketch", "PASS", "Rendered plan captured before ordering", run.final_plan)
        run.selection["contract_number"] = run.kitchen
        total = summary_totals(page, config)["total_price"]
        run.selection["total_price"] = str(total) if total is not None else ""
        return passed
    except (PlaywrightError, ValueError, OSError) as exc:
        run.add("Component selection", "FAIL", f"All-element loop failed: {type(exc).__name__}")
        return False


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
        run.inventory = element_rows(page, config)
        run.final_plan = plan_screenshot(page, config, run, "final-plan")
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


def checkout_check(page, config: dict, run: Run, interactive: bool = False,
                   pre_submit_only: bool = False) -> None:
    """Record the full summary; submit only with complete values and inspect Stripe mode."""
    stage = "order form"
    try:
        page.get_by_role("button", name=label(config, "continue_to_order", "Continue to order")).click(timeout=10000)
        page.locator("#firstName").wait_for(state="visible", timeout=15000)
        if interactive:
            lock_paid_order_button(page, label(config, "paid_order_button", "Complete paid order"))
        contract = page.locator("#contractNumber").input_value()
        run.summary["contract_number"] = contract
        run.add("Order form contract", "MATCH" if contract == run.kitchen else "MISMATCH",
                f"Expected {run.kitchen}; displayed {contract}")
        if contract != run.kitchen:
            return
        listed = element_rows(page, config)
        same_elements = Counter(listed) == Counter(run.inventory)
        run.add("Order summary all elements", "MATCH" if same_elements else "MISMATCH",
                f"Selection: {run.inventory}; order summary: {listed}")
        totals = summary_totals(page, config)
        for key in ("subtotal", "vat", "total_price"):
            value = totals[key]
            if value is not None:
                run.summary[key] = str(value)
            run.add(f"Order summary {key}", "PASS" if value is not None else "FAIL", str(value) if value is not None else "Missing")
        if all(value is not None for value in totals.values()):
            good = totals["subtotal"] + totals["vat"] == totals["total_price"]
            run.add("Order summary arithmetic", "PASS" if good else "FAIL", "Subtotal + VAT = total" if good else "Subtotal + VAT differs from total")
        for key, price in list(run.selection.items()):
            if not key.startswith("option:") or not key.endswith(":price"):
                continue
            name = key[len("option:"):-len(":price")]
            matches = summary_panel(page, config).get_by_text(name, exact=True)
            if matches.count() != 1:
                run.add(f"Order summary option {name}", "FAIL", f"Expected one row; found {matches.count()}")
                continue
            row = matches.first.locator("xpath=../..")
            actual_price = parse_money(row.inner_text())
            quantity = extract(r"[×x]\s*(\d+)", row.inner_text()) or "1"
            run.summary[f"option:{name}:price"] = str(actual_price) if actual_price is not None else ""
            run.summary[f"option:{name}:quantity"] = quantity
            good = actual_price is not None and str(actual_price) == price and quantity == "1"
            run.add(f"Order summary option {name}", "MATCH" if good else "MISMATCH",
                    f"Quantity {quantity}; selected price {price}; summary price {actual_price}")
        if not run.final_plan:
            run.final_plan = plan_screenshot(page, config, run, "final-plan")
        run.add("Pre-submit sketch", "PASS", "Captured before checkout", run.final_plan)
        if interactive:
            prefill_manual_contact(page)
            print("Fill the visible Fragmento form, then press Enter here to verify it.", flush=True)
            print("FORM_READY", flush=True)
            input()
            inspect_manual_contact(page, run)
            run.add("Checkout submission", "NOT_RUN", "Paid-order button disabled in manual inspection")
            return
        defaults = {"first_name": "max", "last_name": "musterman",
                    "contact_email": "ternavaanisa@outlook.com", "phone": "000000000"}
        for key in defaults:
            value = os.getenv(CONTACT_FIELDS[key][0], "").strip() or defaults[key]
            page.locator(CONTACT_FIELDS[key][1]).fill(value)
            valid = page.locator(CONTACT_FIELDS[key][1]).evaluate("el => el.checkValidity()")
            run.summary[f"delivery:{key}"] = value if valid else ""
            run.add(f"Contact {key}", "PASS" if valid else "FAIL", "Valid value displayed" if valid else "Field invalid")
        use_property = os.getenv("FRAGMENTO_USE_PROPERTY_ADDRESS", "true").strip().lower() != "false"
        property_option = page.locator("#use-object-address")
        if property_option.count():
            property_option.set_checked(use_property)
            run.summary["delivery:address_mode"] = "property" if use_property else "custom"
            run.add("Billing/delivery address mode", "PASS", "Property address selected" if use_property else "Custom address selected")
        else:
            use_property = False
        if not use_property:
            needed = ["address_line_1", "country", "city", "postal_code"]
            missing = [CONTACT_FIELDS[key][0] for key in needed if not os.getenv(CONTACT_FIELDS[key][0], "").strip()]
            if missing:
                run.add("Delivery address", "UNCONFIGURED", "Missing local variables: " + ", ".join(missing))
                run.add("Checkout submission", "NOT_RUN", "Address incomplete")
                return
            page.locator("#address1").fill(os.getenv("FRAGMENTO_ADDRESS1", ""))
            for key in ("country", "city", "postal_code"):
                page.locator(CONTACT_FIELDS[key][1]).select_option(label=os.getenv(CONTACT_FIELDS[key][0], ""))
                run.summary[f"delivery:{key}"] = os.getenv(CONTACT_FIELDS[key][0], "")
            run.summary["delivery:address_line_1"] = os.getenv("FRAGMENTO_ADDRESS1", "")
        else:
            run.add("Property address details", "REVIEW", "The page uses the contract address but does not display its fields")
        week = os.getenv("FRAGMENTO_DELIVERY_WEEK", "").strip()
        if week:
            page.locator("#preferredDeliveryDate").click()
            page.get_by_role("option", name=week, exact=True).click(timeout=10000)
        week_value = page.locator("#preferredDeliveryDate").inner_text().strip()
        week_date = page.locator('input[name="preferred-delivery-date"]').input_value()
        run.summary["delivery:delivery_week"] = week_value
        run.add("Preferred delivery", "PASS" if week_date else "FAIL", "Chosen week displayed" if week_date else "No valid week")
        notes = os.getenv("FRAGMENTO_NOTES", "").strip()
        if notes:
            page.locator("#notes").fill(notes)
        run.add("Pre-submit details", "PASS", "Contact fields masked in evidence", masked_checkout_screenshot(page, run))
        if pre_submit_only:
            run.add("Checkout submission", "NOT_RUN", "Stopped at the explicit pre-submit test boundary")
            return
        if any(check.status in {"FAIL", "MISMATCH", "UNCONFIGURED"} for check in run.checks):
            run.add("Checkout submission", "NOT_RUN", "Pre-submit checks must pass before checkout")
            return
        page.locator("#consent").check()
        page.locator("#termsConsent").check()
        run.add("Legal consents", "PASS", "Privacy and terms checkboxes checked for this requested test order")
        page.unroute("**/*")
        checkout_responses: list[str] = []
        page.on("response", lambda response: checkout_responses.append(
            f"{response.request.method} {response.status} {urlparse(response.url).netloc}{urlparse(response.url).path}"
        ) if response.request.method not in ("GET", "HEAD", "OPTIONS") else None)
        stage = "Stripe redirect"
        with page.expect_response(lambda response: urlparse(response.url).path == "/api/orders"
                                  and response.request.method == "POST", timeout=30000) as order_response_info:
            page.get_by_role("button", name=label(config, "paid_order_button", "Complete paid order")).click(timeout=10000)
        order_response = order_response_info.value
        if order_response.status >= 400:
            try:
                payload = order_response.json()
                description = str({key: payload[key] for key in ("code", "message", "error") if key in payload})
            except (ValueError, TypeError, KeyError):
                description = "No structured error detail"
            description = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", description)
            description = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", description)[:300]
            run.add("Order API", "FAIL", f"POST /api/orders returned {order_response.status}; {description}",
                    masked_checkout_screenshot(page, run))
            return
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            candidates = [candidate for candidate in page.context.pages if "checkout.stripe.com" in candidate.url]
            if candidates:
                page = candidates[-1]
                break
            if re.search(r"fragmento\.de/.+(?:confirm|success)", page.url):
                break
            page.wait_for_timeout(500)
        else:
            invalid = [item.get_attribute("id") or "unnamed" for item in page.locator('[aria-invalid="true"]').all()]
            host = urlparse(page.url).netloc
            alerts = [value.strip() for value in page.locator('[role="alert"], [aria-live="assertive"]').all_inner_texts() if value.strip()]
            alerts = [re.sub(r"\d{4,}", "[number]",
                             re.sub(r"[\w.+-]+@[\w.-]+", "[email]", value))[:200] for value in alerts]
            run.add("Checkout redirect", "INCOMPLETE",
                    f"No Stripe/confirmation page after 45 s; visible host {host}; invalid fields {invalid}; "
                    f"write responses {checkout_responses}; alerts {alerts}",
                    masked_checkout_screenshot(page, run) if host == "fragmento.de" else "")
            return
        mode = stripe_mode(page.url)
        if mode != "test":
            run.add("Stripe mode", "INCOMPLETE", "Live or unverified checkout; payment was not attempted")
            run.add("Checkout submission", "INCOMPLETE", "Fragmento form submitted; payment and order confirmation not completed")
            return
        run.add("Stripe mode", "PASS", "Stripe Checkout test session identified by cs_test_ in URL")
        stage = "test payment"
        filled, payment_reason = fill_stripe_test_payment(page)
        if not filled:
            run.add("Test payment", "INCOMPLETE", f"Test payment form unavailable; no Pay click. {payment_reason}")
            return
        page.get_by_role("button", name=re.compile(r"Pay|Place order", re.I)).click(timeout=15000)
        stage = "return from Stripe"
        page.wait_for_url(re.compile(r"^https://fragmento\.de/"), timeout=60000)
        try:
            page.get_by_text(re.compile(r"Bestellnummer|Order (?:number|reference)", re.I)).first.wait_for(
                state="visible", timeout=15000)
        except PlaywrightTimeout:
            pass
        confirmation = page.locator("body").inner_text()
        pattern = config.get("confirmation_reference_pattern", r"(?:Order(?: number| reference)?|Reference|Bestellnummer)\s*[:#]\s*([A-Za-z0-9-]+)")
        run.confirmation_reference = extract(pattern, confirmation)
        confirmed = bool(run.confirmation_reference) and re.search(r"confirm|success|thank|bestätigt|bestaetigt", confirmation, re.I)
        run.add("Order confirmation", "PASS" if confirmed else "REVIEW",
                f"Order reference {run.confirmation_reference}" if confirmed else "Returned from Stripe, but confirmation/reference was unclear",
                screenshot(page, run, "confirmation"))
    except PlaywrightTimeout:
        suffix = "; Fragmento form was clicked, pending-order state unknown" if stage == "Stripe redirect" else ""
        if stage == "return from Stripe":
            metadata = []
            for frame in page.frames:
                for field in frame.locator("input").all()[:20]:
                    if field.is_visible():
                        metadata.append({attribute: field.get_attribute(attribute) for attribute in
                                         ("name", "id", "autocomplete", "required", "aria-invalid")})
            alerts = [value.strip() for value in page.locator('[role="alert"], [aria-live="assertive"]').all_inner_texts() if value.strip()]
            alerts = [re.sub(r"[\w.+-]+@[\w.-]+", "[email]", value)[:200] for value in alerts]
            run.add("Stripe payment result", "INCOMPLETE", f"No return to Fragmento; input metadata {metadata[:20]}; alerts {alerts[:5]}")
        run.add("Checkout", "INCOMPLETE", f"Timed out during {stage}; current host {urlparse(page.url).netloc}{suffix}")
    except PlaywrightError as exc:
        run.add("Checkout", "FAIL", f"Checkout flow failed: {type(exc).__name__}")


def stripe_mode(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "checkout.stripe.com":
        return "unknown"
    if re.search(r"/cs_test_[A-Za-z0-9]+", parsed.path):
        return "test"
    if re.search(r"/cs_live_[A-Za-z0-9]+", parsed.path):
        return "live"
    return "unknown"


def fill_stripe_test_payment(page) -> tuple[bool, str]:
    """Fill only a verified Stripe test checkout. Never capture a payment screenshot."""
    expiry = os.getenv("FRAGMENTO_TEST_EXPIRY", "").strip() or f"12/{(datetime.now().year + 2) % 100:02d}"
    cvc = os.getenv("FRAGMENTO_TEST_CVC", "").strip() or str(secrets.randbelow(900) + 100)
    if not re.fullmatch(r"\d{2}/\d{2}", expiry) or not re.fullmatch(r"\d{3,4}", cvc):
        return False, "Test expiry/CVC format invalid"
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
    except PlaywrightTimeout:
        return False, "Stripe inputs did not appear"
    selectors = {
        "number": ('input[autocomplete="cc-number"]', 'input[name="cardnumber"]', 'input[name="cardNumber"]', 'input[id*="cardNumber"]'),
        "expiry": ('input[autocomplete="cc-exp"]', 'input[name="exp-date"]', 'input[name="cardExpiry"]', 'input[id*="cardExpiry"]'),
        "cvc": ('input[autocomplete="cc-csc"]', 'input[name="cvc"]', 'input[name="cardCvc"]', 'input[id*="cardCvc"]'),
    }
    values = {"number": "4242424242424242", "expiry": expiry, "cvc": cvc}
    for selector, value in (
        ('input[autocomplete="email"]', os.getenv("FRAGMENTO_CONTACT_EMAIL", "ternavaanisa@outlook.com")),
        ('input[autocomplete="cc-name"]', " ".join((os.getenv("FRAGMENTO_FIRST_NAME", "max"),
                                                    os.getenv("FRAGMENTO_LAST_NAME", "musterman")))),
    ):
        for frame in page.frames:
            field = frame.locator(selector)
            if field.count() and field.first.is_visible() and not field.first.input_value().strip():
                field.first.fill(value)
    for key in ("number", "expiry", "cvc"):
        found = False
        for frame in page.frames:
            for selector in selectors[key]:
                locator = frame.locator(selector)
                if locator.count() and locator.first.is_visible():
                    locator.first.fill(values[key])
                    found = True
                    break
            if found:
                break
        if not found:
            available = []
            for frame in page.frames:
                for field in frame.locator("input").all()[:15]:
                    available.append({attribute: field.get_attribute(attribute) for attribute in ("name", "id", "autocomplete", "type")})
            return False, f"Missing {key} input; visible input attributes {available[:20]}"
    return True, "Test card fields filled without recording their values"


def inspect_manual_contact(page, run: Run) -> None:
    property_address = page.locator("#use-object-address").count() and page.locator("#use-object-address").is_checked()
    run.summary["delivery:address_mode"] = "property" if property_address else "custom"
    run.add("Billing/delivery address mode", "PASS", "Property address selected" if property_address else "Custom address selected")
    for key, (_, selector) in CONTACT_FIELDS.items():
        if property_address and key in ("address_line_1", "country", "city", "postal_code"):
            continue
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


from mail_browser import inspect_mail, setup_profile
from visual_qa import render_attachment


def find_browser_email(config: dict, run: Run) -> None:
    recipient = os.getenv("FRAGMENTO_CONTACT_EMAIL", "ternavaanisa@outlook.com")
    result = inspect_mail(MAIL_PROFILE, config, run.confirmation_reference, recipient,
                          run.started.timestamp(), REPORTS / f"{run.started:%Y%m%d-%H%M%S-%f}-{run.kitchen}",
                          os.getenv("FRAGMENTO_BROWSER_CDP_URL", ""))
    run.add("Email", result["status"], result["reason"])
    if result["status"] != "PASS":
        run.add("Email field comparison", "INCOMPLETE", "Requires a confirmed exact-reference message")
        run.add("Email sketch comparison", "INCOMPLETE", "Requires a confirmed exact-reference message and openable plan attachment")
        return
    run.email.update(result.get("fields", {}))
    visible_email = result.get("visible_text", "")
    contract_found = exact_reference(visible_email, run.kitchen)
    run.add("Email contract reference", "MATCH" if contract_found else "REVIEW",
            "Exact contract number visible" if contract_found else "Contract number was not visible in the opened message")
    if contract_found:
        run.email.setdefault("contract_number", run.kitchen)
    for key, value in run.selection.items():
        if key.startswith("option:") and key.endswith(":price"):
            name = key[len("option:"):-len(":price")]
            present = exact_reference(visible_email, name)
            run.add(f"Email element {name}", "PASS" if present else "REVIEW",
                    "Selected element name visible" if present else "Selected element name not found in email text")
    for key, value in run.summary.items():
        if key.startswith("delivery:") and key not in run.email and value and value != "property":
            present = exact_reference(visible_email, value)
            run.add(f"Email {key}", "PASS" if present else "REVIEW",
                    "Summary value visible in email" if present else "Summary value not visible in email")
            if present:
                run.email[key] = value
    if not run.email:
        run.add("Email field comparison", "REVIEW", "No field patterns configured from a real confirmation message")
    for option, labels in run.expected_documents.items():
        for document in labels:
            if document not in config.get("attachment_rules", {}):
                run.add(f"Email attachment {option}: {document}", "REVIEW", "No filename rule from a real email")
            else:
                opened = result.get("documents", {}).get(document, False)
                run.add(f"Email attachment {option}: {document}", "PASS" if opened else "REVIEW",
                        "Matching file downloaded through Outlook UI" if opened else "Matching file could not be opened from Outlook UI")
    attachment = result.get("attachment")
    if not attachment or not run.final_plan:
        run.add("Email sketch comparison", "REVIEW", "Plan attachment could not be opened from the visible mailbox")
        return
    try:
        rendered = render_attachment(attachment)
        rendered_path = evidence_path(run.final_plan).parent / "email-plan-rendered.png"
        rendered_path.write_bytes(rendered)
        rendered_relative = str(rendered_path.relative_to(REPORTS)).replace("\\", "/")
        visual_result = compare_plan_images(evidence_path(run.final_plan), rendered)
        difference = visual_result["changed_fraction"]
        status = "PASS" if visual_result["exact_match"] else "REVIEW"
        run.add("Email sketch comparison", status,
                f"Rendered sketch changed fraction {difference:.4f}; sizes {visual_result['original_sizes']}; nonidentical renderings require review",
                run.final_plan, [rendered_relative])
    except (OSError, ValueError) as exc:
        run.add("Email sketch comparison", "REVIEW", f"Attachment rendering failed: {type(exc).__name__}")


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
    incomplete = {"UNCONFIGURED", "INCOMPLETE", "NOT_RUN", "EMAIL_NOT_FOUND", "REVIEW", "EMAIL_NOT_CHECKED"}
    overall = "FAIL" if any(c.status in bad for c in run.checks) else "INCOMPLETE" if any(c.status in incomplete for c in run.checks) else "PASS"
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for check in run.checks:
        evidence = " ".join(f'<a href="{esc(path)}"><img src="{esc(path)}" alt="{esc(check.name)} screenshot" width="180"></a>'
                            for path in ([check.screenshot] if check.screenshot else []) + check.evidence)
        rows.append(f"<tr><td>{esc(check.name)}</td><td class='{esc(check.status)}'>{esc(check.status)}</td><td>{esc(check.detail)}</td><td>{evidence}</td></tr>")
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Kitchen {esc(run.kitchen)} QA</title>
<style>body{{font:16px system-ui;max-width:1300px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}th,td{{padding:.6rem;border:1px solid #bbb;text-align:left;vertical-align:top}}.PASS,.MATCH{{color:#087333}}.FAIL,.MISMATCH,.NOT_FOUND,.AMBIGUOUS,.LOGIN_FAILURE{{color:#b42318;font-weight:bold}}.UNCONFIGURED,.INCOMPLETE,.NOT_RUN,.EMAIL_NOT_FOUND,.EMAIL_NOT_CHECKED,.REVIEW{{color:#955600}}</style>
<h1>Kitchen {esc(run.kitchen)} — {overall}</h1><p>Started: {esc(run.started.isoformat(timespec='seconds'))}</p>
<table><thead><tr><th>Check</th><th>Status</th><th>Details</th><th>Evidence</th></tr></thead><tbody>{''.join(rows)}</tbody></table></html>"""
    path.write_text(document, encoding="utf-8")
    safe_summary = {key: value for key, value in run.summary.items() if not key.startswith("delivery:") or
                    key in {"delivery:address_mode", "delivery:delivery_week"}}
    path.with_suffix(".json").write_text(json.dumps({
        "kitchen": run.kitchen, "selection": run.selection, "summary": safe_summary,
        "inventory": run.inventory, "expected_documents": run.expected_documents,
        "final_plan": run.final_plan, "confirmation_reference": run.confirmation_reference,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_order_source(path: Path, kitchen: str, run: Run) -> None:
    """Restore non-sensitive order evidence for an email-only run."""
    snapshot = path if path.suffix.lower() == ".json" else path.with_suffix(".json")
    if snapshot.is_file():
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        if data.get("kitchen") != kitchen:
            raise ValueError("Source report belongs to another kitchen")
        run.selection.update(data.get("selection", {}))
        run.summary.update(data.get("summary", {}))
        run.inventory = data.get("inventory", [])
        run.expected_documents = data.get("expected_documents", {})
        run.final_plan = data.get("final_plan", "")
        run.confirmation_reference = data.get("confirmation_reference", "")
    else:
        document = path.read_text(encoding="utf-8")
        if f"Kitchen {html.escape(kitchen)} QA" not in document:
            raise ValueError("Source report belongs to another kitchen")
        rows = re.findall(r"<tr><td>(.*?)</td><td class='[^']+'>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>",
                          document, re.S)
        for raw_name, _status, raw_detail, _evidence in rows:
            name = html.unescape(re.sub(r"<[^>]+>", "", raw_name))
            detail = html.unescape(re.sub(r"<[^>]+>", "", raw_detail))
            if name.startswith("Selection vs order summary "):
                key = name[len("Selection vs order summary "):]
                match = re.fullmatch(r"Selection: (.*?); summary: (.*)", detail, re.S)
                if match:
                    run.selection[key], run.summary[key] = match.groups()
            elif name in {"Order summary subtotal", "Order summary vat"}:
                run.summary[name.removeprefix("Order summary ")] = detail
            elif name == "Final element inventory":
                match = re.search(r"found (\[.*\])", detail, re.S)
                if match:
                    run.inventory = ast.literal_eval(match.group(1))
        folder = REPORTS / path.stem
        final_plan = folder / "final-plan.png"
        if final_plan.is_file():
            run.final_plan = str(final_plan.relative_to(REPORTS)).replace("\\", "/")
        confirmation = folder / "confirmation.png"
        if confirmation.is_file():
            run.add("Prior confirmation screen", "REVIEW", "Visible confirmation from source run; reference supplied separately",
                    str(confirmation.relative_to(REPORTS)).replace("\\", "/"))
    for key in ("first_name", "last_name", "contact_email", "phone"):
        configured_value = os.getenv(CONTACT_FIELDS[key][0], "").strip()
        if configured_value:
            run.summary[f"delivery:{key}"] = configured_value
    run.add("Order source", "PASS", f"Loaded saved selection and sketch from {path.name}", run.final_plan)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fragmento public kitchen visual QA checker")
    parser.add_argument("--kitchen", help="Kitchen number to check")
    parser.add_argument("--config", type=Path, default=ROOT / "workflow.json")
    parser.add_argument("--setup-mail-browser", action="store_true", help="Sign in to the reusable Outlook browser profile")
    parser.add_argument("--interactive", action="store_true", help="Open the public form visibly for manual contact entry")
    parser.add_argument("--pre-submit-only", action="store_true", help="Run visual and form checks without starting checkout")
    parser.add_argument("--email-only", action="store_true", help="Check email for an existing confirmed order without a new checkout")
    parser.add_argument("--order-reference", help="Exact reference of an already confirmed order for --email-only")
    parser.add_argument("--from-report", type=Path, help="Saved confirmed-order report for --email-only")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if args.setup_mail_browser:
        setup_profile(MAIL_PROFILE, "https://outlook.live.com/mail/")
        return 0
    if args.email_only and (not args.order_reference or not args.from_report):
        parser.error("--email-only requires --order-reference and --from-report")
    if args.from_report and not args.email_only:
        parser.error("--from-report requires --email-only")
    try:
        config = read_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    kitchen = (args.kitchen if args.kitchen is not None else input("Enter purchase contract / kitchen number: ")).strip()
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
    if args.email_only:
        try:
            load_order_source(args.from_report, kitchen, run)
        except (OSError, ValueError, SyntaxError, json.JSONDecodeError) as exc:
            print(f"Order source error: {exc}", file=sys.stderr)
            return 2
        reference = args.order_reference.strip()
        if run.confirmation_reference and run.confirmation_reference != reference:
            print("Order reference differs from the saved report", file=sys.stderr)
            return 2
        run.confirmation_reference = reference
        run.add("Existing order reference", "REVIEW", "Provided reference; matching email must corroborate this order")
    else:
        public = config.get("public", config.get("selection", {}))
        if check_url(public.get("url", "")):
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=not args.interactive)
                    context = browser.new_context()
                    page = context.new_page()
                    public_contract_check(page, public, run, interactive=args.interactive,
                                          visual=config.get("visual", {}), pre_submit_only=args.pre_submit_only)
                    context.close()
                    browser.close()
            except (PlaywrightError, OSError, ValueError) as exc:
                run.add("Browser", "FAIL", f"Browser could not start: {type(exc).__name__}")
        else:
            run.add("Public kitchen", "UNCONFIGURED", "Set the public Fragmento URL in workflow.json")
    find_browser_email(config.get("email", {}), run)
    compare(run)
    path = write_report(run)
    overall = "FAIL" if any(c.status in {"FAIL", "MISMATCH", "NOT_FOUND", "AMBIGUOUS", "LOGIN_FAILURE"} for c in run.checks) else "INCOMPLETE" if any(c.status in {"UNCONFIGURED", "INCOMPLETE", "NOT_RUN", "EMAIL_NOT_FOUND", "EMAIL_NOT_CHECKED", "REVIEW"} for c in run.checks) else "PASS"
    print(f"Kitchen {kitchen}: {overall}")
    for check in run.checks:
        print(f"- {check.name}: {check.status}" + (f" — {check.detail}" if check.detail else ""))
    print(f"Report: {path}")
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
