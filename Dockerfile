# Containerised web front end for the GTM/GA4 audit tool.
#
# The base image ships the matching Chromium build and all OS deps, so no
# `playwright install` layer is needed — but its tag MUST match the playwright
# version pinned in gtm_verifier/requirements.txt (bump both together).
#
#   docker build -t gtm-verifier .
#   docker run --rm -e PORT=8080 -p 8080:8080 gtm-verifier
#
# Deployed to Cloud Run with ./deploy.sh — see notes/cloud-followups.md for
# which service flags stand in for the code fixes still outstanding.
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

# Always headless in a container (no display); browser launch args already
# include --no-sandbox / --disable-dev-shm-usage (see gtm_verifier/browser.py).
ENV HEADLESS=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY gtm_verifier/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
WORKDIR /app/gtm_verifier

# One worker, few threads: each audit request drives a full Chromium session,
# and config.py's module globals are only safe while one audit runs per process
# (Cloud Run enforces that with --concurrency 1; see notes/cloud-followups.md #2).
#
# --timeout 3600 matches Cloud Run's maximum request timeout: audits run inline
# in the request thread, so gunicorn must never be the one to give up first.
#
# Shell form (not exec form) so ${PORT} expands — Cloud Run injects it and
# ignores the container's own choice. `exec` keeps gunicorn as PID 1 so SIGTERM
# still reaches it at instance shutdown.
CMD exec gunicorn --workers 1 --threads 2 \
    --timeout 3600 --graceful-timeout 30 \
    --access-logfile - --error-logfile - \
    --bind "0.0.0.0:${PORT:-8080}" webapp.app:app
