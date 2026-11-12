"""Checks that each product's websites/apps are up."""
import time

import httpx

from .config import settings

_last_state: dict = {}


async def check_all():
    """Return list of dicts: brand, url, ok, status, ms."""
    results = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
        for brand in settings.brands.values():
            for url in brand.urls:
                t = time.monotonic()
                try:
                    r = await http.get(url)
                    ok = r.status_code < 400
                    status = r.status_code
                except Exception as e:
                    ok, status = False, type(e).__name__
                results.append({"brand": brand.name, "url": url, "ok": ok,
                                "status": status, "ms": int((time.monotonic() - t) * 1000)})
    return results


def format_results(results) -> str:
    if not results:
        return "ما في روابط مراقبة مضافة (SEEN_MONITOR_URLS / NVOLVE_MONITOR_URLS)."
    lines = []
    for r in results:
        icon = "✅" if r["ok"] else "🔴"
        lines.append(f"{icon} {r['brand']} — {r['url']} ({r['status']}, {r['ms']}ms)")
    return "\n".join(lines)


def changes(results):
    """Return results whose up/down state changed since the last check."""
    changed = []
    for r in results:
        prev = _last_state.get(r["url"])
        if prev is not None and prev != r["ok"]:
            changed.append(r)
        _last_state[r["url"]] = r["ok"]
    return changed
