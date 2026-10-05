"""Gas price fetcher with daily history for Wattson.

Scrapes AAA for the configured metro area's Regular average. The AAA page
URL and the metro label are configurable via environment variables so
non-California users can point at their own state's page:

  WATTSON_GAS_PRICE_URL   (default: https://gasprices.aaa.com?state=CA)
  WATTSON_GAS_PRICE_METRO (default: Los Angeles-Long Beach)

Set WATTSON_GAS_PRICE to pin a price explicitly (no scraping). Useful for
demo mode, offline use, or regions AAA doesn't cover.

Prices are cached per Pacific/local day in gas_price_history.json; the AAA
site is hit at most once per day. If the scrape fails, the fallback price
is cached too, so a AAA outage can't stall dashboard generation.
"""
import os
import re
import json
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

CACHE_PATH = Path(os.environ.get(
    "WATTSON_GAS_CACHE",
    str(Path(__file__).parent / "gas_price_history.json")))

AAA_URL = os.environ.get(
    "WATTSON_GAS_PRICE_URL", "https://gasprices.aaa.com?state=CA")
AAA_METRO = os.environ.get(
    "WATTSON_GAS_PRICE_METRO", "Los Angeles-Long Beach")
DEFAULT_GAS_PRICE = 4.50


def _local_tz():
    return ZoneInfo(os.environ.get("WATTSON_TIMEZONE", "America/Los_Angeles"))


def _fetch_aaa_metro():
    """Scrape the AAA state page for the configured metro's Regular price."""
    req = urllib.request.Request(
        AAA_URL,
        headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    metro_idx = html.find(AAA_METRO)
    if metro_idx == -1:
        return None
    section = html[metro_idx:metro_idx + 3000]
    for m in re.finditer(r"<tr>(.*?)</tr>", section, re.DOTALL):
        row = m.group(1)
        if "Current Avg" in row:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(tds) >= 2:
                price_txt = re.sub(r"<[^>]+>", "", tds[1]).strip()
                pm = re.search(r"\$([0-9]+\.[0-9]+)", price_txt)
                if pm:
                    return float(pm.group(1))
    return None


def _load_history():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_history(hist):
    try:
        CACHE_PATH.write_text(json.dumps(hist))
    except Exception:
        pass


def _fallback_price():
    return float(os.environ.get(
        "WATTSON_GAS_PRICE",
        os.environ.get("RIVIAN_GAS_PRICE", DEFAULT_GAS_PRICE)))


def get_gas_price(for_date=None):
    """Return gas price for a date (YYYY-MM-DD). Defaults to today.

    An explicit WATTSON_GAS_PRICE always wins (no scraping). Otherwise the
    AAA page is fetched at most once per day; failures cache the fallback
    so one outage can't trigger a scrape per charge. Historical dates
    without cached data use today's price."""
    # Explicit override: no network at all.
    pinned = os.environ.get("WATTSON_GAS_PRICE")
    if pinned:
        try:
            return float(pinned)
        except ValueError:
            pass

    tz = _local_tz()
    if for_date is None:
        for_date = datetime.now(tz).date().isoformat()
    today = datetime.now(tz).date().isoformat()

    hist = _load_history()
    if for_date in hist:
        return float(hist[for_date]["price"])

    # Ensure we have today's price (fetch if needed, at most once per day).
    if today not in hist:
        price = None
        try:
            price = _fetch_aaa_metro()
        except Exception:
            pass
        if price:
            hist[today] = {"price": price,
                           "source": "AAA %s" % AAA_METRO}
        else:
            # Cache the fallback so a AAA outage costs one attempt per day,
            # not one 15s timeout per historical charge.
            hist[today] = {"price": _fallback_price(), "source": "fallback"}
        _save_history(hist)

    if today in hist:
        return float(hist[today]["price"])
    return _fallback_price()
