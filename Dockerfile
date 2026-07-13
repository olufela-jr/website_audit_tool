# Containerised web front end for the GTM/GA4 audit tool.
#
# The base image ships the matching Chromium build and all OS deps, so no
# `playwright install` layer is needed — but its tag MUST match the playwright
# version pinned in gtm_verifier/requirements.txt (bump both together).
#
#   docker build -t gtm-verifier .
#   docker run --rm -p 8000:8000 gtm-verifier
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

# Always headless in a container (no display); browser launch args already
# include --no-sandbox / --disable-dev-shm-usage (see gtm_verifier/browser.py).
ENV HEADLESS=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY gtm_verifier/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . /app
WORKDIR /app/gtm_verifier

# One worker, few threads: each audit request drives a full Chromium session.
# Long --timeout because a full audit request runs for minutes (see
# notes/cloud-followups.md for the background-jobs follow-up).
CMD ["gunicorn", "--workers", "1", "--threads", "2", "--timeout", "600", \
     "-b", "0.0.0.0:8000", "webapp.app:app"]
