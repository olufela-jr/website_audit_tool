# Website Audit Tool — GTM / GA4 dataLayer Verifier

Verifies that a site's Google Tag Manager / GA4 analytics are deployed and firing
correctly. It drives a headless Chrome session, watches `window.dataLayer` and the
GA4 network traffic, validates required fields on each event, and produces either a
colour-coded terminal report or a client-ready PowerPoint deck.

## What it checks

**Infrastructure audits** — work on any site, no configuration of selectors needed:

| Audit | Module | What it checks |
|-------|--------|----------------|
| `analytics_audit` | `analytics.py` | GA4 / GTM are present and correctly deployed |
| `consent_audit`   | `consent.py`   | CMP banner, Consent Mode signals, pre-/post-consent GA4 firing |
| `network_audit`   | `network.py`   | GA4 `collect` requests via CDP — client ID, session ID, consent state, event inventory, session timeline |

**Journey audits** — simulate user flows and assert the expected ecommerce / form
events fire. These require CSS selectors for the target site (see Configuration):

`page_load`, `consent`, `shop`, `product_detail`, `cart`, `checkout`,
`purchase`, `subscribe`, `contact`, `search`

Each check has a severity (CRITICAL / HIGH / MEDIUM / LOW / INFO). Scores are
equal-weighted (PASS = 1, FAIL = 0); SKIP and INFO checks are excluded from the
denominator.

## Setup

Requires Python 3.9+ and a local install of Google Chrome. Selenium 4.6+ ships
with Selenium Manager, so chromedriver is downloaded automatically.

```bash
cd website_audit_tool
python3 -m venv venv
source venv/bin/activate
pip install -r gtm_verifier/requirements.txt
```

## Configuration

```bash
cd gtm_verifier
cp config.example.yaml config.yaml
```

Then edit `config.yaml` with the target site's URLs, GTM/GA4 IDs, and CSS
selectors. `config.yaml` is git-ignored so client-specific values stay local; the
three infrastructure audits run without any selectors filled in.

To wire up a journey, open the site in Chrome, right-click the relevant element in
DevTools → **Copy → Copy selector**, and paste it into `config.yaml`. The only
selector confirmed against the demo site so far is the consent accept button.

## Usage

Run from inside `gtm_verifier/` with the venv active.

```bash
# Terminal output (default)
python run.py                          # all audits
python run.py analytics_audit          # one audit
python run.py page_load consent shop   # several audits

# PowerPoint export
python run.py --export report.pptx                 # all audits → deck
python run.py analytics_audit --export audit.pptx  # specific audits → deck

# Custom config file
python run.py --config staging.yaml --export report.pptx
```

Recommended order: start with the infrastructure audits (they work immediately),
then fill in selectors and run the journeys.

```bash
python run.py analytics_audit consent_audit network_audit   # step 1
python run.py page_load consent                             # step 2 (after selectors)
```

The process exits non-zero if any non-INFO check fails, so it can gate CI.

## Project layout

```
gtm_verifier/
  run.py              CLI entry point and journey dispatch
  config.py           loads config.yaml into module constants
  config.example.yaml configuration template (copy to config.yaml)
  core.py             driver, dataLayer polling, field validation, scoring, report
  analytics.py        analytics_audit
  consent.py          consent_audit
  network.py          network_audit (CDP traffic capture)
  journeys.py         the interactive journey audits
  export.py           PowerPoint export
```