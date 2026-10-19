# Fragmento public kitchen QA checker

This tool follows the **public customer journey** on `fragmento.de`. It takes a purchase contract number, chooses the configured language and Text instructions, opens the kitchen, checks its plan, exercises available Add options, opens the customer order form, and compares the visible pre-submit summary with the selected options. It stops before **Complete paid order**. It does not use Admin or a dashboard.

## Desktop app

After the one-time setup below, double-click [start_fragmento.cmd](start_fragmento.cmd), or run:

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Enter the purchase contract number and click **Nis kontrollin**. The app lists each result and opens the HTML report with **Hap raportin**. Use **Të dhënat e kontaktit** to fill the local `.env` form; its values are not shown in the results table or report. The app runs the same `check_kitchen.py` engine as the command line, so existing reports and safety limits still apply. `INCOMPLETE` is expected because the final paid-order action is intentionally not run.

The flow was inspected with contract `111105807`, which opens `ab-105807`. The page shows an order form after **Continue to order**. The final button starts a paid order through Stripe, so a normal check never clicks it. No official test checkout was visible in the public flow.

When you click **Nis kontrollin**, a visible browser opens the public site. Choose the options you want there, then click **Zgjodha opsionet · Vazhdo** in the app. The app checks each available choice by changing and restoring it, then opens the real Fragmento contact and delivery form. Fill that form using the site's live dropdown choices and click **E plotësova formën · Verifiko**. The local contact editor is optional and only prefills the real form. The paid-order button is disabled during this check; the app never requests card number or CVV.

## Windows PowerShell setup

From this project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item workflow.example.json workflow.json
Copy-Item .env.example .env
notepad .env
```

If activation is blocked, run `Set-ExecutionPolicy -Scope Process Bypass` in that PowerShell window and activate again. `.env`, `workflow.json`, `.auth/`, and `reports/` are excluded from Git. Keep all contact details in `.env`, not in source code.

Set these checkout fields in `.env` for automatic prefill or command-line checks. In the desktop app, you may fill the live form directly instead:

| Variable | Visible Fragmento field | Example format |
| --- | --- | --- |
| `FRAGMENTO_FIRST_NAME` | First name | Text |
| `FRAGMENTO_LAST_NAME` | Last name | Text |
| `FRAGMENTO_CONTACT_EMAIL` | Email | Email address that should receive an order confirmation |
| `FRAGMENTO_PHONE` | Phone | International phone number |
| `FRAGMENTO_ADDRESS1` | Address (street, no.) | Street and number |
| `FRAGMENTO_COUNTRY` | Country | Visible dropdown label, such as `Poland` |
| `FRAGMENTO_CITY` | City | Visible dropdown label for that country, such as `Warsaw` |
| `FRAGMENTO_POSTAL_CODE` | Postal code | Visible dropdown label for that city, such as `00-001` |
| `FRAGMENTO_DELIVERY_WEEK` | Preferred delivery week | `After 4 weeks`, `After 5 weeks`, or `After 6 weeks` in English |

`FRAGMENTO_ADDRESS2` and `FRAGMENTO_NOTES` are optional. The country, city, and postal code must be choices offered by the site; the program reports a form failure if a configured choice is unavailable. The program fills these fields and reads them back but does not agree to terms or submit the order. Reports say whether each field matches local configuration without printing personal values. Screenshots of the filled form mask inputs, selects, and notes. Do not put card numbers, CVV, payment passwords, or payment tokens in `.env`.

Set `public.language` in `workflow.json`. The included values are for the inspected **English** flow. For another language, also set its displayed `instruction_label`, `intro_continue_label`, `kitchen_heading`, `confirm_button`, and translated `labels` entries. Other translations have not been verified. `known_kitchens` includes the verified example mapping; new contract numbers use the public contract entry lookup, so no source edit is needed.

Run:

```powershell
python check_kitchen.py --kitchen 111105807
# or, to be prompted:
python check_kitchen.py
```

Each run writes a timestamped HTML file in `reports\` plus screenshots. A normal safe run is `INCOMPLETE` overall because submission is deliberately `NOT_RUN`. Public checks and pre-submit comparisons receive their own PASS/FAIL status. The command exits with code 1 for incomplete or failed runs.

## Existing confirmation email

An email can only be compared if it **already exists** for the exact contract number (or a verified order reference). The tool never uses the newest unrelated email. It does not place an order to create an email.

To read an Outlook.com inbox, set `FRAGMENTO_MS_CLIENT_ID` to a Microsoft public client app ID and `FRAGMENTO_MAIL_ADDRESS` to the account being authorized, such as `ternavaanisa@outlook.com`. The app must support personal Microsoft accounts and delegated `Mail.Read` and `User.Read`. Then authorize once:

```powershell
python check_kitchen.py --setup-mail
```

The read-only token cache stays in `.auth/`. Normal runs use it silently. In `workflow.json`, add `email.field_patterns` after inspecting a **real matching confirmation email**. Keys should match summary keys such as `contract_number`, `total_price`, `option:Lighting set with 3 LED spotlights:price`, or `delivery:city`; values are regular expressions with a first or named `value` capture group. For example: `"total_price": "^Total price:\\s*(?P<value>.+)$"`. An absent, ambiguous, or inaccessible email never counts as a match. No card or payment credential fields are extracted or reported.

For attachments, set `email.attachment_rules` in `workflow.json` using document labels shown on selected components (such as `E-Label PDF`) and filename regular expressions learned from a real email. Set `email.plan_attachment_pattern` for the plan attachment. The checker reads attachments only from the exact matching message. It compares plan bytes when the public file and attachment share a format; different formats remain `INCOMPLETE` until a visual comparison method is configured. Different bytes alone do not prove the drawings differ. Without a matching email and these rules, attachment and sketch equality are not claimed.

Microsoft documents the [Graph message API](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0), [attachment listing](https://learn.microsoft.com/en-us/graph/api/message-list-attachments?view=graph-rest-1.0), and [device code sign-in](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-acquire-token-device-code-flow).

## Check limits

- The tool verifies contract number in the opened URL and order form, the kitchen slug and plan label, image loading, Add to Remove state changes, component count, option prices, subtotal, VAT, and total. It saves before and after selection screenshots.
- It exercises enabled Add options visible in the catalog. Fixed, already confirmed, and other custom controls are displayed in the screenshots but need separate business rules for automated interaction checks.
- It confirms that the visible order summary agrees with the selected options and that locally configured contact fields display as entered. The site does not show a separate final confirmation before the paid-order button.
- An actual submitted order, Stripe payment, final confirmation screen, and newly generated email require an authorized official test checkout or an existing order. None is claimed by a normal run.
- Screenshots show appearance and state; judging whether a rendered design is visually correct needs an approved reference image or explicit design rules.
