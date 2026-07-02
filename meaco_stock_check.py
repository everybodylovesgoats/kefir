#!/usr/bin/env python3

"""
Stock checker for the Meaco Cirro portable air conditioners.

meaco.com runs on Shopify, so the most reliable way to check stock is the
Shopify product JSON endpoint (the product URL with ".js" on the end). That
returns an exact "available": true/false flag per product, which is far more
trustworthy than scraping "out of stock" / "add to cart" wording off the page
(those buttons all exist in the markup at once and are toggled by JavaScript).

Sends a phone push via ntfy.sh when any watched model comes back in stock.
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from html import unescape

# -----------------------------
# Settings
# -----------------------------

# Phone push notifications via ntfy.sh.
# The topic is read from the NTFY_TOPIC environment variable and is NOT stored
# in this file, so the repository can be public without exposing it. Set it as
# a GitHub Actions secret named NTFY_TOPIC (and subscribe your phone's ntfy app
# to the same topic). If unset, notifications are silently skipped.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# The Meaco Cirro air conditioners to watch (name + Shopify handle).
PRODUCTS = [
    {
        "name": "Meaco Cirro 12000 BTU - Cooling Only",
        "handle": "meaco-cirro-12000-btu-super-quiet-smart-portable-air-conditioner",
    },
    {
        "name": "Meaco Cirro 12000 BTU - Cooling & Heating",
        "handle": "meaco-cirro-12000-btu-super-quiet-smart-portable-air-conditioner-heater",
    },
    {
        "name": "Meaco Cirro+ 14000 BTU - Cooling Only",
        "handle": "meaco-cirro-14000-btu-super-quiet-inverter-smart-portable-air-conditioner",
    },
    {
        "name": "Meaco Cirro+ 14000 BTU - Cooling & Heating",
        "handle": "meaco-cirro-14000-btu-super-quiet-inverter-smart-portable-air-conditioner-heater",
    },
    {
        "name": "Meaco Cirro+ 16000 BTU - Cooling Only",
        "handle": "meaco-cirro-16000-btu-super-quiet-inverter-smart-portable-air-conditioner",
    },
    {
        "name": "Meaco Cirro+ 16000 BTU - Cooling & Heating",
        "handle": "meaco-cirro-16000-btu-super-quiet-inverter-smart-portable-air-conditioner-heater",
    },
]

BASE_URL = "https://www.meaco.com/products/"

# Where the "already alerted" memory is stored.
# On GitHub Actions this is set to a file the workflow caches between runs.
STATE_FILE = os.path.expanduser(
    os.environ.get("MEACO_STATE_FILE", "~/.meaco_stock_seen.json")
)
REQUEST_TIMEOUT_SECONDS = 20

# Alerting behaviour:
# - the very first time stock is found: send FIRST_TIME_ALERT_COUNT alerts
# - checks 2 up to ALERT_EVERY_TIME_LIMIT: one alert each
# - after that, send REPEAT_ALERT_COUNT alerts on every run while it stays in stock
# - if a product goes out of stock, its memory is cleared so a later restock
#   starts the count again from zero.
FIRST_TIME_ALERT_COUNT = 3
ALERT_EVERY_TIME_LIMIT = 5
REPEAT_ALERT_COUNT = 2


# -----------------------------
# Helpers
# -----------------------------

def load_seen():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


def fetch_url(url, timeout=REQUEST_TIMEOUT_SECONDS):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        ),
        "Accept-Language": "en-GB,en;q=0.9",
    }
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return raw.decode("utf-8", errors="ignore"), None
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return "", f"URL error: {e.reason}"
    except Exception as e:
        return "", f"Error: {e}"


def clean_text(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


def check_via_shopify_json(handle):
    """
    Preferred method: ask Shopify for the product JSON and read the exact
    availability. Returns (status, reason) or None if it couldn't be used
    (so we can fall back to HTML).
    """
    html, error = fetch_url(BASE_URL + handle + ".js")
    if error or not html:
        return None

    try:
        data = json.loads(html)
    except Exception:
        return None

    variants = data.get("variants") or []

    # These air conditioners have a single variant, so the product-level
    # "available" flag is enough; but we also treat "any variant available"
    # as in stock to be safe.
    product_available = bool(data.get("available"))
    any_variant_available = any(v.get("available") for v in variants)

    if product_available or any_variant_available:
        return "IN_STOCK", "Shopify reports available"
    return "OUT_OF_STOCK", "Shopify reports not available"


def check_via_html(handle):
    """
    Fallback method: read the product page and use schema.org availability,
    which is more reliable than the visible button text on these pages.
    """
    html, error = fetch_url(BASE_URL + handle)
    if error or not html:
        return "UNKNOWN", f"could not fetch product page ({error or 'empty'})"

    lower = html.lower()
    if "schema.org/instock" in lower:
        return "IN_STOCK", "schema.org says InStock"
    if any(x in lower for x in ("schema.org/outofstock", "schema.org/soldout")):
        return "OUT_OF_STOCK", "schema.org says out of stock"

    text = clean_text(html)
    if "out of stock" in text or "sold out" in text:
        return "OUT_OF_STOCK", "page shows out-of-stock wording"
    if (
        "add to cart" in text
        or "add to basket" in text
        or "buy now" in text
    ):
        return "POSSIBLE_IN_STOCK", "page shows an add-to-cart / buy-now button"

    return "UNKNOWN", "could not determine stock from page"


def assess_product(handle):
    result = check_via_shopify_json(handle)
    if result is not None:
        return result[0], result[1], "Shopify JSON"
    status, reason = check_via_html(handle)
    return status, reason, "HTML fallback"


def phone_notify(title, message, click_url=None):
    """
    Sends a push notification to your phone via ntfy.sh.
    Install the free "ntfy" app and subscribe to NTFY_TOPIC.
    Fails silently if there's no network.
    """
    if not NTFY_TOPIC:
        return
    try:
        url = f"https://ntfy.sh/{NTFY_TOPIC}"
        headers = {
            "Title": title,
            "Priority": "high",
            "Tags": "package",
        }
        if click_url:
            headers["Click"] = click_url
        request = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(request, timeout=15)
    except Exception:
        pass


def mac_notify(title, message):
    """Shows a Mac notification. Harmlessly does nothing on the cloud runner."""
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass


def open_in_browser(url):
    """Opens the URL in a browser on a Mac. Does nothing on the cloud runner."""
    try:
        subprocess.run(["open", url], check=False)
    except Exception:
        pass


def send_alert(title, message, url):
    """Fires one alert (phone push + Mac notification)."""
    mac_notify(title, message)
    phone_notify(title, message, click_url=url)


# -----------------------------
# Main check
# -----------------------------

def main():
    seen = load_seen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\nMeaco Cirro stock check: {now}")
    print("-" * 70)

    any_in_stock = False

    for product in PRODUCTS:
        name = product["name"]
        handle = product["handle"]
        url = BASE_URL + handle

        status, reason, method = assess_product(handle)
        print(f"[{status}] ({method}) {name} - {reason}")
        print(f"  {url}")

 

