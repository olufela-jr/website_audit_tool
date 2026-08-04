# Website Audit Tool — GTM / GA4 Verifier

Audits a site's Google Tag Manager / GA4 implementation. It drives a Chromium
session (via Playwright), watches `window.dataLayer` and GA4 network traffic, validates required
fields on each event, and produces either a colour-coded terminal report or a
client-ready PowerPoint deck.

Works in **two modes**:

- **Foreign site** — point it at any public URL, no configuration at all. Runs
  the seven infrastructure audits (useful for prospect audits / first contact).
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
| `ga4_config`      | `remote_config.py` | GA4 property settings read from the public `gtag.js` remote config — key events, cross-domain list, enhanced measurement, Google signals |
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

Requires Python 3.9+. Playwright downloads its own bundled Chromium, so no
system Chrome or chromedriver install is needed.

```bash
cd website_audit_tool
python3 -m venv venv
source venv/bin/activate
pip install -r gtm_verifier/requirements.txt
python -m playwright install chromium
```

To drive an installed Google Chrome instead of the bundled Chromium (e.g. when
a site's bot protection treats them differently), set `BROWSER_CHANNEL=chrome`.
How sites tell the two apart — and when each is the right choice — is written
up in `chromium-vs-chrome.txt`. (Not available in the deployed container, which
only ships bundled Chromium.)

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

## Deployment

The web front end runs on Cloud Run (project `project-atlas-audit`, region
`europe-west2`). Access is Google sign-in plus an email allowlist, handled in
the app (`webapp/auth.py`) — manage it with `./users.sh add someone@example.com`.
Ship a revision with `./deploy.sh`; Cloud Build builds the `Dockerfile`, so no
local Docker is required. Setup steps and log commands are in `HOWTORUN.txt`.

Audit targets are restricted to public internet hosts: `net_guard.py` resolves
each URL and refuses private, loopback and link-local addresses (the cloud
metadata endpoint in particular). `ALLOW_PRIVATE_TARGETS=1` lifts that for
local staging work.

The service is configured as single-tenant per instance (`--concurrency 1`) and
holds results in process memory. Several `deploy.sh` flags stand in for bugs
that are still open in the code — read `notes/cloud-followups.md` before
changing any of them.

## Project layout

```
gtm_verifier/
  run.py                CLI entry point and audit dispatch
  config.py             tolerant YAML config loader (every key optional)
  net_guard.py          SSRF guard — public-internet targets only
  config.example.yaml   client config template with the journey schema
  palm_view_config.yaml verified worked example (Palm View demo site)
  core.py               driver, persistent dataLayer recorder, polling,
                        field validation, consent auto-accept, scoring, report
  journeys.py           declarative journey engine + audit wrappers
  analytics.py          analytics_audit
  consent.py            consent_audit
  network.py            network_audit (CDP traffic capture)
  remote_config.py      ga4_config (GA4 property settings from public gtag.js)
  tags_inventory.py     tag_inventory
  seo.py                seo
  security_headers.py   security_headers
  export.py             PowerPoint export
  webapp/               Flask front end (URL + optional config upload)
    auth.py             Google sign-in + ALLOWED_USERS allowlist
Dockerfile              container image (Playwright base + gunicorn)
deploy.sh               deploy the web front end to Cloud Run
users.sh                add/remove who can sign in
notes/cloud-followups.md  what the Cloud Run flags are standing in for
```
