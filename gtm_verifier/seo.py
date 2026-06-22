"""
SEO & metadata audit — publicly observable, no site access required.

Fetches the page's server-rendered HTML (what crawlers see) plus robots.txt and
sitemap.xml, and checks the on-page SEO fundamentals: title, meta description,
canonical, Open Graph, viewport, headings, structured data, indexability,
hreflang and language. Pure HTTP — no browser, no third-party deps (uses lxml,
already a dependency).
"""

import json
from typing import List
from urllib.parse import urljoin, urlparse

from lxml import html as lxml_html

from core import CheckResult, Severity, failed_check
from httpfetch import fetch


def _ok(name, detail, sev=Severity.MEDIUM) -> CheckResult:
    return CheckResult(name=name, event=None, passed=True, detail=detail, severity=sev)


def _bad(name, detail, sev=Severity.MEDIUM) -> CheckResult:
    return CheckResult(name=name, event=None, passed=False, detail=detail, severity=sev)


def run_seo_audit(url: str) -> List[CheckResult]:
    page = fetch(url)
    if not page.ok:
        return [failed_check("SEO fetch", f"Could not fetch page: {page.error}", Severity.HIGH)]

    try:
        doc = lxml_html.fromstring(page.text)
    except Exception as exc:  # noqa: BLE001
        return [failed_check("SEO parse", f"Could not parse HTML: {exc}", Severity.HIGH)]

    results: List[CheckResult] = []

    def first(xpath):
        found = doc.xpath(xpath)
        return found[0].strip() if found else ""

    # ── Title ──────────────────────────────────────────────────────────────
    title = first("//head/title/text()")
    if not title:
        results.append(_bad("Title tag", "No <title> found", Severity.HIGH))
    elif not (10 <= len(title) <= 65):
        results.append(_bad(
            "Title tag",
            f"Title length {len(title)} chars (aim ~10–60): {title!r}",
            Severity.MEDIUM,
        ))
    else:
        results.append(_ok("Title tag", f"{len(title)} chars: {title!r}", Severity.HIGH))

    # ── Meta description ─────────────────────────────────────────────────────
    desc = first("//meta[@name='description']/@content")
    if not desc:
        results.append(_bad("Meta description", "No meta description", Severity.HIGH))
    elif not (50 <= len(desc) <= 165):
        results.append(_bad(
            "Meta description",
            f"Length {len(desc)} chars (aim ~50–160)", Severity.MEDIUM,
        ))
    else:
        results.append(_ok("Meta description", f"{len(desc)} chars", Severity.HIGH))

    # ── Indexability (robots meta) ───────────────────────────────────────────
    robots_meta = first("//meta[@name='robots']/@content").lower()
    if "noindex" in robots_meta:
        results.append(_bad(
            "Indexable", f"Page is noindex via meta robots: {robots_meta!r}", Severity.CRITICAL,
        ))
    else:
        results.append(_ok(
            "Indexable", robots_meta or "no robots meta (indexable by default)", Severity.HIGH,
        ))

    # ── Canonical ────────────────────────────────────────────────────────────
    canonical = first("//link[@rel='canonical']/@href")
    if canonical:
        results.append(_ok("Canonical link", canonical, Severity.MEDIUM))
    else:
        results.append(_bad("Canonical link", "No rel=canonical link", Severity.MEDIUM))

    # ── Viewport (mobile) ────────────────────────────────────────────────────
    viewport = first("//meta[@name='viewport']/@content")
    if viewport:
        results.append(_ok("Mobile viewport", viewport, Severity.MEDIUM))
    else:
        results.append(_bad("Mobile viewport", "No viewport meta — not mobile-friendly", Severity.MEDIUM))

    # ── H1 ───────────────────────────────────────────────────────────────────
    h1s = [t.strip() for t in doc.xpath("//h1//text()") if t.strip()]
    h1_count = len(doc.xpath("//h1"))
    if h1_count == 0:
        results.append(_bad("H1 heading", "No <h1> on page", Severity.MEDIUM))
    elif h1_count > 1:
        results.append(_bad("H1 heading", f"{h1_count} <h1> tags (prefer one)", Severity.LOW))
    else:
        results.append(_ok("H1 heading", (h1s[0] if h1s else "(empty)"), Severity.MEDIUM))

    # ── html lang ────────────────────────────────────────────────────────────
    lang = first("//html/@lang")
    results.append(
        _ok("Language attribute", lang, Severity.LOW) if lang
        else _bad("Language attribute", "No lang attribute on <html>", Severity.LOW)
    )

    # ── Open Graph ───────────────────────────────────────────────────────────
    og = {
        p.split(":", 1)[1]: c
        for p, c in (
            (el.get("property", ""), el.get("content", ""))
            for el in doc.xpath("//meta[starts-with(@property,'og:')]")
        )
        if p.startswith("og:")
    }
    missing_og = [k for k in ("title", "description", "image") if not og.get(k)]
    if not og:
        results.append(_bad("Open Graph", "No og: tags (poor social sharing)", Severity.MEDIUM))
    elif missing_og:
        results.append(_bad("Open Graph", f"Missing og:{', og:'.join(missing_og)}", Severity.LOW))
    else:
        results.append(_ok("Open Graph", "og:title, og:description, og:image present", Severity.MEDIUM))

    # ── Structured data (JSON-LD) ────────────────────────────────────────────
    types = []
    for node in doc.xpath("//script[@type='application/ld+json']/text()"):
        try:
            data = json.loads(node)
        except Exception:  # noqa: BLE001 — malformed JSON-LD is common; skip
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict) and obj.get("@type"):
                t = obj["@type"]
                types.extend(t if isinstance(t, list) else [t])
    results.append(_ok(
        "Structured data (JSON-LD)",
        f"{len(types)} item(s): {', '.join(map(str, types))}" if types else "None found",
        Severity.INFO,
    ))

    # ── hreflang ─────────────────────────────────────────────────────────────
    hreflangs = doc.xpath("//link[@rel='alternate']/@hreflang")
    results.append(_ok(
        "hreflang",
        f"{len(hreflangs)} alternate(s): {', '.join(hreflangs)}" if hreflangs else "None (single-locale)",
        Severity.INFO,
    ))

    # ── robots.txt ───────────────────────────────────────────────────────────
    origin = f"{urlparse(page.final_url).scheme}://{urlparse(page.final_url).netloc}"
    robots = fetch(urljoin(origin, "/robots.txt"))
    if robots.ok and robots.status == 200 and robots.text.strip():
        sitemap_in_robots = "sitemap:" in robots.text.lower()
        results.append(_ok(
            "robots.txt",
            "present" + (" (declares Sitemap)" if sitemap_in_robots else " (no Sitemap declared)"),
            Severity.MEDIUM,
        ))
    else:
        results.append(_bad("robots.txt", "Not found or empty", Severity.LOW))

    # ── sitemap.xml ──────────────────────────────────────────────────────────
    sitemap = fetch(urljoin(origin, "/sitemap.xml"))
    if sitemap.ok and sitemap.status == 200 and "<" in sitemap.text:
        results.append(_ok("sitemap.xml", "present at /sitemap.xml", Severity.MEDIUM))
    else:
        results.append(_bad(
            "sitemap.xml", "Not at /sitemap.xml (may be declared elsewhere in robots.txt)", Severity.LOW,
        ))

    return results
