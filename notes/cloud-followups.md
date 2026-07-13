# Cloud deployment follow-ups

The Dockerfile makes the app *runnable* in a container (headless Chromium via
the Playwright base image, gunicorn instead of the Flask dev server). The items
below are what still stands between "runs in a container" and "safe to expose
as a multi-user cloud service". None are designed yet — this is the checklist.

## 1. Synchronous audits vs HTTP timeouts
`POST /audit` runs every audit inline in the request thread; a full passive
audit takes ~1–2 min and journeys many minutes. Most platforms (Cloud Run,
ALB/nginx defaults) cut HTTP requests at 30–60 s. The gunicorn `--timeout 600`
papers over this for a single box only. Real fix: background jobs
(Cloud Tasks / RQ / Celery) + a status-polling or SSE results page.

## 2. Global config state is not concurrency-safe
`config.py` module globals (BASE_URL, JOURNEYS, CONSENT_ACCEPT_BUTTON, …) are
mutated per request via `config.load_dict()` / `set_base_url()`
(webapp/app.py). Two simultaneous users clobber each other's target/selectors
mid-run. Fix: a per-run config object threaded through the audit functions.
Until then keep gunicorn at 1 worker / low threads and treat the app as
single-tenant.

## 3. In-process results cache breaks with >1 instance
`_RUNS` (webapp/app.py) holds results for the .pptx download in process memory.
With multiple workers/instances the download can land on an instance that
never ran the audit → 404. Fix: shared storage (Redis / GCS / disk) or
regenerate-on-download.

## 4. No auth + user-supplied URLs = SSRF/abuse surface
Anyone who can reach the app can drive a real browser at any URL (including
cloud metadata endpoints / internal services), and the "authorized" checkbox
gating side-effecting journeys is honour-system. Fix before exposing: put
authentication in front (IAP / OAuth proxy / basic auth at minimum), deny
private/link-local address ranges, and consider an allowlist for journey
targets.

## 5. Committed client config must not ship in the image
`gtm_verifier/config.yaml` (auto-loaded on import!) and
`palm_view_config.yaml` contain a real client's site URL and GTM/GA4 IDs. A
deployed instance would default to auditing that site. Fix: exclude client
YAMLs from the image (.dockerignore), rely on runtime upload / env config
only.

## 6. Platform choice (deferred)
The Dockerfile is platform-agnostic. When picking a platform, mind: request
timeout limits (see #1), RAM ≥ 2 GB per instance (Chromium), instance
concurrency = 1 until #2 is fixed, and `/dev/shm` size (launch args already
pass --disable-dev-shm-usage).
