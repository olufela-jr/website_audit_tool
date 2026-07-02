# Website Audit Tool — GTM / GA4 Verifier

Audits a site's Google Tag Manager / GA4 implementation. It drives a Chrome
session, watches `window.dataLayer` and GA4 network traffic, validates required
fields on each event, and produces either a colour-coded terminal report or a
client-ready PowerPoint deck.

Works in **two modes**:

- **Foreign site** — point it at any public URL, no configuration at all. Runs
  the six infrastructure audits (useful for prospect audits / first contact).
- **Client site** — a per-client YAML config adds expected GTM/GA4 IDs to
  verify and declarative **journeys** that walk the site and assert the
  dataLayer events their tagging is supposed to push. Onboarding a new client
  is pure YAML — no code changes.

## What it checks

**Infrastructure audits** — work on any site, nothing but a URL needed:

| Audit | Module | What it checks |
|-------|--------|----------------|
| `analytics_audit` | `analytics.py` | GA4/GTM presence and deployment method; verifies expected IDs when configured |
| `consent_audit`   | `consent.py`   | CMP banner, Consent Mode v2 signals, pre-/post-consent GA4 firing |
| `network_audit`   | `network.py`   | GA4 `collect` requests via CDP — client ID, session ID, consent state, event inventory |
| `tag_inventory`   | `tags_inventory.py` | All marketing/analytics tags and pixels on the page |
| `seo`             | `seo.py`       | SEO & metadata checks |
| `security_headers`| `security_headers.py` | HTTP security header checks |

**Journeys** — defined per site in the YAML config as steps
(`goto` / `click` / `type` / `select_index` / `accept_consent` / `mark`) plus
`expect` blocks naming the dataLayer event, required fields (dot-notation), and
optional regex patterns. See `config.example.yaml` for the schema and
`palm_view_config.yaml` for a complete worked example against the demo site.

Events pushed by onclick handlers immediately before a full-page navigation are
captured: a recorder injected into every document mirrors dataLayer pushes into
`sessionStorage`, which survives same-origin navigations.

Each check has a severity (CRITICAL / HIGH / MEDIUM / LOW / INFO). Scores are
equal-weighted; SKIP and INFO checks are excluded from the denominator. The
process exits non-zero if any non-INFO check fails, so it can gate CI.

## Setup

Requires Python 3.9+ and a local install of Google Chrome. Selenium 4.6+ ships
with Selenium Manager, so chromedriver is downloaded automatically.

```bash
cd website_audit_tool
python3 -m venv venv
source venv/bin/activate
pip install -r gtm_verifier/requirements.txt
```

## Usage

Run from inside `gtm_verifier/` with the venv active.

```bash
# Foreign site — no config file needed
python run.py --url https://prospect.com
python run.py --url https://prospect.com analytics_audit consent_audit

# Client site — config.yaml auto-loads; --config for others
python run.py                              # all audits + configured journeys
python run.py --config client.yaml
python run.py --config client.yaml shop cart          # specific journeys
python run.py --url https://staging.client.com --config client.yaml

python run.py --list                       # available audits + journeys
python run.py --export report.pptx         # PowerPoint deck
HEADLESS=1 python run.py ...               # no visible Chrome window
```

## Onboarding a new client site

```bash
cp config.example.yaml client.yaml
```

1. Set `site.base_url` (and `tags.gtm_id` / `tags.ga4_id` if known — the
   analytics audit then verifies those exact containers are live).
2. Set `selectors.consent.accept_button` only if the built-in CMP
   auto-detection (OneTrust, Cookiebot, Didomi, Quantcast, generic accept
   text, consent iframes) misses their banner.
3. Write `journeys:` for the flows that matter. Selectors: DevTools →
   right-click element → **Copy → Copy selector**.
4. `python run.py --config client.yaml`

## Web front end

```bash
flask --app webapp.app run        # from gtm_verifier/; HEADLESS=1 optional
```

Open http://localhost:5000. Enter a URL for the public/authorized audits, or
upload a client config YAML to also run its journeys and verify expected tag
IDs — same no-code onboarding as the CLI. Results render as HTML with a
PowerPoint download.

## Project layout

```
gtm_verifier/
  run.py                CLI entry point and audit dispatch
  config.py             tolerant YAML config loader (every key optional)
  config.example.yaml   client config template with the journey schema
  palm_view_config.yaml verified worked example (Palm View demo site)
  core.py               driver, persistent dataLayer recorder, polling,
                        field validation, consent auto-accept, scoring, report
  journeys.py           declarative journey engine + audit wrappers
  analytics.py          analytics_audit
  consent.py            consent_audit
  network.py            network_audit (CDP traffic capture)
  tags_inventory.py     tag_inventory
  seo.py                seo
  security_headers.py   security_headers
  export.py             PowerPoint export
  webapp/               Flask front end (URL + optional config upload)
```
