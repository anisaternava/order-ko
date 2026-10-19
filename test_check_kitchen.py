import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import check_kitchen as checker


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

    def test_attachment_rules_apply_only_to_exact_matching_message(self):
        run = checker.Run("12345")
        run.expected_documents = {"Lighting": ["E-Label PDF"]}
        message = {"id": "message-1", "hasAttachments": True}
        config = {"attachment_rules": {"E-Label PDF": r"label.*\.pdf$"}}
        attachments = {"value": [{"name": "lighting-label.pdf", "contentType": "application/pdf"}]}
        with patch.object(checker, "graph_get", return_value=attachments) as graph:
            checker.check_email_attachments(config, run, message, "token")
        graph.assert_called_once_with(
            "https://graph.microsoft.com/v1.0/me/messages/message-1/attachments", "token")
        statuses = {check.name: check.status for check in run.checks}
        self.assertEqual(statuses["Email document Lighting: E-Label PDF"], "PASS")
        self.assertEqual(statuses["Email plan file"], "UNCONFIGURED")


if __name__ == "__main__":
    unittest.main()
