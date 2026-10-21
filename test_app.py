import unittest
from pathlib import Path

from app import parse_checker_output


class DesktopOutputTests(unittest.TestCase):
    def test_cli_results_include_names_with_colons(self):
        output = (
            "Kitchen 111105807: INCOMPLETE\n"
            "- Selection vs order summary option:Lighting set:price: MATCH — Selection: 69.00; summary: 69.00\n"
            "- Checkout submission: NOT_RUN — Stopped before payment\n"
            "- Visual Lighting: REVIEW — No clear plan change\n"
            "Report: C:\\reports\\example.html\n"
        )
        overall, checks, report = parse_checker_output(output)
        self.assertEqual(overall, "INCOMPLETE")
        self.assertEqual(checks[0][:2], ("Selection vs order summary option:Lighting set:price", "MATCH"))
        self.assertEqual(checks[1][1], "NOT_RUN")
        self.assertEqual(checks[2][1], "REVIEW")
        self.assertEqual(report, Path("C:\\reports\\example.html"))


if __name__ == "__main__":
    unittest.main()
