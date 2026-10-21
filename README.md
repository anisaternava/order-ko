# Fragmento public visual QA

`check_kitchen.py` follows the public customer journey at `https://fragmento.de/`. It does not use Admin, Microsoft Graph, or a browser extension. Supply any purchase contract number. The public page determines the kitchen and available components.

## Windows PowerShell setup

```powershell
cd C:\Users\User\Documents\order-ko
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item workflow.example.json workflow.json
Copy-Item .env.example .env
notepad .env
```

If the virtual environment already exists, activate it and install the updated requirements. `.env`, `workflow.json`, `.auth/`, and `reports/` are excluded from Git.

Set `public.language` and its translated labels in `workflow.json` if you do not use English. The checker reads the kitchen identifier from the public result, contract query parameter, plan label, and order form. `visual.fixed_plan_reference` can point to an approved image of the starting kitchen, and `visual.plan_references` can map option names to approved images after selection. Without those references the program reports `REVIEW` for semantic placement, even when pixels change.

The requested test contact values are the local defaults: `max`, `musterman`, `ternavaanisa@outlook.com`, `000000000`. Override them only in your private `.env` with `FRAGMENTO_FIRST_NAME`, `FRAGMENTO_LAST_NAME`, `FRAGMENTO_CONTACT_EMAIL`, and `FRAGMENTO_PHONE`. Set `FRAGMENTO_USE_PROPERTY_ADDRESS=true` to choose the site's property/contract address option. For a custom address, set it to `false` and configure `FRAGMENTO_ADDRESS1`, `FRAGMENTO_COUNTRY`, `FRAGMENTO_CITY`, and `FRAGMENTO_POSTAL_CODE` using the visible site choices. `FRAGMENTO_DELIVERY_WEEK` is optional; when blank the site's displayed default is used. `FRAGMENTO_NOTES` is optional.

## Run

The safe pre-submit verification is:

```powershell
python check_kitchen.py --kitchen 111105807 --pre-submit-only
```

For a new authorized test order, the full run submits the Fragmento form and follows its Stripe redirect:

```powershell
python check_kitchen.py --kitchen 111105807
```

Without `--kitchen`, the program prompts `Enter purchase contract / kitchen number:`. Each run writes an HTML report and screenshots under `reports\`. A nonzero exit code means FAIL or INCOMPLETE. An overall PASS requires successful order confirmation, matching email, and all visual/data checks.

The checker discovers summary rows and enabled Add/Remove cards for the current kitchen. It checks each selectable card's state, preserves the other elements, compares name/count/price and the rendered plan before/after, and leaves intended options selected. Fixed or already confirmed rows are inventoried but their placement is marked `REVIEW` without an approved visual reference. An unchanged plan also produces `REVIEW`; a successful click does not prove the drawing is correct.

## Stripe payment boundary

The script accepts the privacy/terms checkboxes for the requested test order. After `Complete paid order`, it identifies a test Stripe Checkout session only from a `cs_test_` session identifier on `checkout.stripe.com`. If it sees `cs_live_` or cannot verify the mode, it stops before Pay. Clicking the Fragmento button may create a pending order even when payment is not completed. The test card number is held in code only for a verified test session. Supply a valid future expiry and test CVC for that PowerShell process, not in Git or the report:

```powershell
$env:FRAGMENTO_TEST_EXPIRY='12/34'
$env:FRAGMENTO_TEST_CVC='123'
python check_kitchen.py --kitchen 111105807
Remove-Item Env:\FRAGMENTO_TEST_EXPIRY,Env:\FRAGMENTO_TEST_CVC
```

Payment fields are never put in screenshots, traces, or reports. If Stripe's visible fields or redirect cannot be automated, the result is INCOMPLETE. Stripe [documents test cards and test mode](https://docs.stripe.com/testing) and [notes limits of automating Checkout](https://docs.stripe.com/automated-testing).

## Existing signed-in Outlook browser

A new Playwright browser does not inherit a login from an ordinary already-open browser. To create a reusable visible Outlook session, run:

```powershell
python check_kitchen.py --setup-mail-browser
```

Sign in inside the opened Outlook window and press Enter in PowerShell. The browser profile remains in ignored `.auth\outlook-browser`. No email password is entered into this program. Alternatively, set `FRAGMENTO_BROWSER_CDP_URL` to a local Chromium remote-debugging endpoint that you launched with the desired signed-in profile. The tool only reads that browser's visible Outlook UI.

After a **confirmed test order with an order reference**, the checker searches Outlook for that exact reference and checks the recipient. It waits up to `email.wait_seconds` in `workflow.json`. It never treats the newest message as a match. Configure `email.field_patterns` from an actual message to extract fields for comparison with the order summary, and `email.plan_attachment_pattern` to find the sketch attachment by its visible name. It attempts to download that attachment through Outlook's UI and render PDF/image files for visual comparison. If sign-in, reference, attachment, or visual evidence is unavailable, it reports `EMAIL_NOT_CHECKED`, `REVIEW`, or `INCOMPLETE`, not PASS.

To check an already placed order without submitting or paying again, use the saved checkout report:

```powershell
python check_kitchen.py --kitchen 111105807 --email-only --order-reference 111105807-2 --from-report reports\20261020-234211-233381-111105807.html
```

This command reads the saved selection, summary, and plan, then searches Outlook. It makes no Fragmento checkout call. Leave the setup browser closed after signing in so the same profile can be reopened for the check.

## Current result for 111105807

The public site opened `ab-105807`. The final order included the lighting set for €69, with one unit; pickup was tested and then deselected because the fixed assembly service makes the two incompatible. The displayed subtotal €57.98 plus VAT €11.02 equaled €69.00. A verified Stripe `cs_test_` checkout accepted the test payment, and Fragmento showed **Bestellung bestätigt**, order **111105807-2**. See [the saved checkout report](reports/20261020-234211-233381-111105807.html) and its confirmation screenshot. The original report labels confirmation `REVIEW` because it read the page before the reference appeared; the screenshot contains the reference, and the wait/parser was fixed afterward. The plan did not visibly change when lighting or pickup was toggled, so visual placement remains `REVIEW` without an approved reference. Outlook email, attachment, and sketch comparisons remain `EMAIL_NOT_CHECKED`/`INCOMPLETE` until the reusable Outlook profile is signed in and an exact-reference message is found.
