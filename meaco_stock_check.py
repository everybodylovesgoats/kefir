#!/usr/bin/env python3

"""
Stock checker for a single Meaco product, used to TEST the alerting logic:
    MeacoDry Arete One 6L  (Warm Pebble variant)
    https://www.meaco.com/products/meacodry-arete-one-6l?variant=56834319155587

This is a copy of the air-conditioner checker (meaco_stock_check.py) with the
SAME alerting logic, just pointed at the dehumidifier. Because that item is
currently in stock, running this repeatedly is a good way to confirm the alert
cadence works end to end:

  - first time stock is found:            FIRST_TIME_ALERT_COUNT alerts (3)
  - checks 2 up to ALERT_EVERY_TIME_LIMIT: one alert each
  - after that:                            REPEAT_ALERT_COUNT alerts every run (2)
  - if it ever goes out of stock, its memory is cleared so a later restock
    starts the count again from zero.

meaco.com runs on Shopify, so the most reliable way to check stock is the
Shopify product JSON endpoint (the product URL with ".js" on the end). That
returns each colour variant with an exact "available": true/false flag, which
is far more trustworthy than scraping "out of stock" wording off the page.

Sends a phone push via ntfy.sh when the watched item is in stock.
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
# Install the free "ntfy" app on your phone and subscribe to this exact topic.
# (Can also be overridden with an NTFY_TOPIC environment variable.)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "Barn3389")

# The single product to watch (the dehumidifier we tested earlier).
PRODUCTS = [
    {
        "name": "MeacoDry Arete One 6L - Warm Pebble",
        "handle": "meacodry-arete-one-6l",
        # The exact colour variant to watch (the "?variant=..." number in the
        # URL). Set to None to treat the product as in stock if ANY variant is.
        "variant_id": 56834319155587,
    },
]

BASE_URL = "https://www.meaco.com/products/"

# Where the "already alerted" memory is stored.
# On GitHub Actions this is set to a file the workflow caches between runs.
STATE_FILE = os.path.expanduser(
    os.environ.get("MEACO_STATE_FILE", "~/.meaco_dehumidifier_seen.json")
)
REQUEST_TIMEOUT_SECONDS = 20

# Alerting behaviour (identical to the air-conditioner script):
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


def check_via_shopify_json(handle, variant_id=None):
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

    # If a specific colour variant is requested, read only that one.
    if variant_id is not None:
        target = next(
            (v for v in variants if str(v.get("id")) == str(variant_id)),
            None,
        )
        if target is None:
            return "UNKNOWN", "target variant id not found in product JSON"
        colour = target.get("title", "variant")
        if target.get("available"):
            return "IN_STOCK", f"Shopify reports '{colour}' available"
        return "OUT_OF_STOCK", f"Shopify reports '{colour}' not available"

    # Otherwise: in stock if the product, or any variant, is available.
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


def assess_product(handle, variant_id=None):
    result = check_via_shopify_json(handle, variant_id)
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
            "Tags": "sweat_droplets",
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

    print(f"\nMeaco dehumidifier stock check: {now}")
    print("-" * 70)

    any_in_stock = False

    for product in PRODUCTS:
        name = product["name"]
        handle = product["handle"]
        variant_id = product.get("variant_id")

        url = BASE_URL + handle
        if variant_id is not None:
            url = f"{url}?variant={variant_id}"

        status, reason, method = assess_product(handle, variant_id)
        print(f"[{status}] ({method}) {name} - {reason}")
        print(f"  {url}")

        in_stock = status in ("IN_STOCK", "POSSIBLE_IN_STOCK")

        if in_stock:
            any_in_stock = True

            # Count how many consecutive checks this product has been in stock.
            record = seen.get(url) or {"count": 0}
            record["count"] = record.get("count", 0) + 1
            record["status"] = status
            record["last_seen"] = now
            seen[url] = record
            count = record["count"]

            # First time: several alerts. Next few checks: one each.
            # After that: repeat every run.
            if count == 1:
                n_alerts = FIRST_TIME_ALERT_COUNT
            elif count <= ALERT_EVERY_TIME_LIMIT:
                n_alerts = 1
            else:
                n_alerts = REPEAT_ALERT_COUNT

            title = "MeacoDry Arete One 6L in stock!"
            message = f"{name} is available. Buy now.\n{url}"
            for _ in range(n_alerts):
                send_alert(title, message, url)
            open_in_browser(url)  # open the page once, not once per alert

            print(f"  -> in stock for {count} check(s); sent {n_alerts} alert(s)")

        elif status == "OUT_OF_STOCK":
            # Went out of stock: clear its memory so a future restock alerts
            # again from the very first check.
            if url in seen:
                del seen[url]
                print("  -> was previously in stock; memory cleared")
        # UNKNOWN status: leave memory untouched and don't alert.

    print("-" * 70)
    if not any_in_stock:
        print("No in-stock Meaco dehumidifier found.")

    save_seen(seen)


if __name__ == "__main__":
    main()

