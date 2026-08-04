# Cloud deployment follow-ups

The app is deployed to Cloud Run (`./deploy.sh`, project `project-atlas-audit`,
region `europe-west2`) behind IAP. That deployment was a deliberate lift-and-
shift: the items below were **mitigated by service configuration, not fixed in
the code**. Every one of them is still a live defect in the app itself, and
every mitigation is a flag in `deploy.sh` that must not be dropped casually.

Status legend: MITIGATED = still broken in code, contained by config.
DONE = no longer an issue. OPEN = neither fixed nor contained.

## 1. Synchronous audits vs HTTP timeouts — MITIGATED
`POST /audit` runs every audit inline in the request thread; a full passive
audit takes ~1–2 min and journeys many minutes.
*Mitigation:* Cloud Run `--timeout 3600` (its maximum) and gunicorn
`--timeout 3600` to match, so neither gives up first. The existing running
overlay in `index.html` covers the wait.
*Residual:* the operator's tab must stay open for the whole run, and there are
no retries — a dropped connection loses the work. A journey suite that runs
past 60 min cannot complete at all. Real fix remains background jobs
(Cloud Tasks) + a status-polling or SSE results page.

## 2. Global config state is not concurrency-safe — MITIGATED
`config.py` module globals (BASE_URL, JOURNEYS, CONSENT_ACCEPT_BUTTON, …) are
mutated per request via `config.load_dict()` / `set_base_url()`
(webapp/app.py). `network.LAST_RUN` is shared the same way. Two simultaneous
requests in one process clobber each other's target mid-run.
*Mitigation:* Cloud Run `--concurrency 1` — one request per instance, and
instances are separate processes, so parallel users cannot collide. Gunicorn
stays at 1 worker / 2 threads.
*Residual:* the code is still unsafe. Raising `--concurrency` above 1, or adding
gunicorn workers, silently reintroduces the bug with no error to notice. Real
fix: a per-run config object threaded through the audit functions.

## 3. In-process results cache breaks with >1 instance — MITIGATED
`_RUNS` (webapp/app.py) holds results for the .pptx download in process memory,
so the download must land on the instance that ran the audit.
*Mitigation:* `--session-affinity` routes a returning client back to its
instance.
*Residual:* affinity is best-effort — it lapses if the instance is recycled or
scaled down, and the download then 404s with "this report has expired". Real
fix: shared storage (GCS / Firestore) or regenerate-on-download.

## 4. No auth + user-supplied URLs = SSRF/abuse surface — DONE (both halves)

**Auth.** Google OAuth in the app itself (`webapp/auth.py`) plus an
`ALLOWED_USERS` allowlist; manage it with `./users.sh`. The app refuses to
start unless auth is configured or `AUTH_DISABLED=1` is set explicitly, so
`--allow-unauthenticated` on the service cannot leave it open.

This replaced IAP, which could not work here: Cloud Run's integrated IAP
authenticates against a Google-managed OAuth client and so admits only
identities inside the project's own organisation. Approved users on gmail.com
and assemblyglobal.com were rejected regardless of their IAM binding, and a
custom OAuth client cannot be attached to Cloud Run's integrated IAP —
`gcloud beta iap web enable --oauth2-client-id` accepts only `app-engine` and
`backend-services`. The remaining IAP route was an external load balancer with
its own OAuth client; app-level auth was chosen over that.

**SSRF.** `net_guard.py` resolves every target and refuses private, loopback,
link-local (169.254.169.254), multicast, unspecified and reserved addresses,
plus non-http(s) schemes and the cloud metadata hostnames. Resolution is the
point: a hostname anyone can register may point at 127.0.0.1, so checking the
string proves nothing. Enforced at the web form, the CLI entry point, every
raw fetch *including each redirect hop*, the browser fetch, and journey `goto`
steps. `ALLOW_PRIVATE_TARGETS=1` opts out for local staging work.

*Residual:*
- **DNS rebinding.** The check happens at request time; a name whose answer
  changes between check and connect can still slip through. Closing it needs
  connection-level pinning in both urllib and Chromium.
- **Sub-resources.** A public page that embeds `<img src="http://169.254...">`
  still causes the browser to issue that request. No response body reaches the
  operator, so this leaks little, but it is not blocked.
- The "authorized" checkbox gating side-effecting journeys remains
  honour-system.

## 5. Committed client config must not ship in the image — DONE
`gtm_verifier/config.yaml` (auto-loaded on import) and `palm_view_config.yaml`
carry a real client's URL and GTM/GA4 IDs. Both are excluded by `.dockerignore`
(image) and `.gcloudignore` (Cloud Build upload) — the latter matters because
`palm_view_config.yaml` is committed, so `.gitignore` would not have caught it.
Config reaches a deployed instance only via runtime upload.

## 6. Platform choice — DONE
Cloud Run, `europe-west2`. Sized in `deploy.sh`: 4 GiB / 2 vCPU (Chromium needs
≥ 2 GB and /tmp is tmpfs on top of it), gen2 execution environment for a real
/dev/shm, `--cpu-boost` because cold start includes launching a browser, scale
to zero with `--max-instances 4`. Launch args already pass
`--disable-dev-shm-usage` (gtm_verifier/browser.py).

## 7. No CI/CD — OPEN
Deploys are manual (`./deploy.sh` from a laptop, Cloud Build does the build).
There is no test gate before a deploy and no build on push. If deploys become
frequent or shared, add a Cloud Build trigger on the GitHub repo that runs
`pytest` before `gcloud run deploy`.
