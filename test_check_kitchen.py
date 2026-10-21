import tempfile
import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from PIL import Image

import check_kitchen as checker
from mail_browser import reference_present
from visual_qa import compare_plan_images


class KitchenCheckerTests(unittest.TestCase):
    def test_price_parser_stays_on_same_line_as_euro_amount(self):
        card = "Article: KA220043_S3\n69,00\u00a0€\nAdd +"
        self.assertEqual(checker.parse_money(card), Decimal("69.00"))

    def test_currency_display_normalization(self):
        self.assertEqual(checker.comparable("total_price", "1.234,50 €"),
                         checker.comparable("total_price", "1234.50"))
        self.assertNotEqual(checker.comparable("total_price", "69,00 €"),
                            checker.comparable("total_price", "70.00"))

    def test_reference_requires_exact_identifier(self):
        self.assertTrue(checker.exact_reference("Order for kitchen 12345", "12345"))
        self.assertFalse(checker.exact_reference("Order for kitchen 123456", "12345"))
        self.assertFalse(checker.exact_reference("Order for kitchen A12345", "12345"))

    def test_summary_comparison_ignores_formatting_but_not_real_difference(self):
        run = checker.Run("12345")
        run.selection = {"option:model:price": "69.00", "contract_number": "12345"}
        run.summary = {"option:model:price": "69.00", "contract_number": "123456"}
        checker.compare(run)
        self.assertEqual({c.name: c.status for c in run.checks},
                         {"Selection vs order summary option:model:price": "MATCH",
                          "Selection vs order summary contract_number": "MISMATCH"})

    def test_missing_summary_is_incomplete(self):
        run = checker.Run("12345")
        run.selection = {"total_price": "69.00"}
        checker.compare(run)
        self.assertEqual(run.checks[0].status, "INCOMPLETE")

    def test_email_is_not_compared_without_verified_match(self):
        run = checker.Run("12345")
        run.summary = {"total_price": "69.00"}
        run.email = {"total_price": "70.00"}
        checker.compare(run)
        self.assertFalse(any(c.name.startswith("Email vs") for c in run.checks))

    def test_report_escapes_fields_and_marks_missing_email(self):
        run = checker.Run("12345")
        run.add("Email", "EMAIL_NOT_FOUND", "No <matching> email")
        with tempfile.TemporaryDirectory() as temp, patch.object(checker, "REPORTS", Path(temp)):
            report = checker.write_report(run).read_text(encoding="utf-8")
        self.assertIn("Kitchen 12345", report)
        self.assertIn("INCOMPLETE", report)
        self.assertIn("No &lt;matching&gt; email", report)

    def test_email_only_source_restores_order_without_browser(self):
        original = checker.Run("12345")
        original.selection = {"contract_number": "12345", "total_price": "69.00"}
        original.summary = dict(original.selection)
        original.final_plan = "sample/final-plan.png"
        original.confirmation_reference = "12345-2"
        with tempfile.TemporaryDirectory() as temp, patch.object(checker, "REPORTS", Path(temp)):
            report = checker.write_report(original)
            restored = checker.Run("12345")
            checker.load_order_source(report, "12345", restored)
        self.assertEqual(restored.selection, original.selection)
        self.assertEqual(restored.confirmation_reference, "12345-2")
        self.assertEqual(restored.final_plan, "sample/final-plan.png")

    def test_stripe_mode_requires_explicit_test_session(self):
        self.assertEqual(checker.stripe_mode("https://checkout.stripe.com/c/pay/cs_test_abc123"), "test")
        self.assertEqual(checker.stripe_mode("https://checkout.stripe.com/c/pay/cs_live_abc123"), "live")
        self.assertEqual(checker.stripe_mode("https://fragmento.de/cs_test_abc123"), "unknown")
        self.assertEqual(checker.stripe_mode("https://evilstripe.com/c/pay/cs_test_abc123"), "unknown")

    def test_mail_reference_requires_exact_order_token(self):
        self.assertTrue(reference_present("Order AB-123 confirmed", "AB-123"))
        self.assertFalse(reference_present("Order AB-1234 confirmed", "AB-123"))

    def test_plan_comparison_detects_changed_pixels(self):
        def png(color):
            output = BytesIO()
            Image.new("RGB", (20, 20), color).save(output, "PNG")
            return output.getvalue()
        self.assertEqual(compare_plan_images(png("white"), png("white"))["changed_fraction"], 0)
        self.assertGreater(compare_plan_images(png("white"), png("black"))["changed_fraction"], 0.9)


if __name__ == "__main__":
    unittest.main()
