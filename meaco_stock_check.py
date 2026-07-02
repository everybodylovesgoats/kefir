#!/usr/bin/env python3

"""
Stock checker for a single Meaco product:
    MeacoDry Arete One 6L  (Warm Pebble variant)
    https://www.meaco.com/products/meacodry-arete-one-6l?variant=56834319155587

meaco.com runs on Shopify, so the most reliable way to check stock is the
Shopify product JSON endpoint (the product URL with ".js" on the end). That
returns each colour variant with an exact "available": true/false flag, which
is far more trustworthy than scraping "out of stock" wording off the page.

Sends a phone push via ntfy.sh when the variant comes back in stock.
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
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "Barn3389")

# The product to watch.
PRODUCT_NAME = "MeacoDry Arete One 6L (Warm Pebble)"
PRODUCT_HANDLE = "meacodry-arete-one-6l"
PRODUCT_URL = f"https://www.meaco.com/products/{PRODUCT_HANDLE}"

# The exact colour variant to watch. This is the "?variant=..." number in the
# URL you gave (Warm Pebble). Set to None to alert if ANY variant is in stock.
TARGET_VARIANT_ID = 56834319155587

# Where the "already alerted" memory is stored.
# On GitHub Actions this is set to a file the workflow caches between runs.
STATE_FILE = os.path.expanduser(
    os.environ.get("MEACO_STATE_FILE", "~/.meaco_arete_seen.json")
)
REQUEST_TIMEOUT_SECONDS = 20


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


def check_via_shopify_json():
    """
    Preferred method: ask Shopify for the product JSON and read the exact
    per-variant availability. Returns (status, reason) or None if it couldn't
    be used (so we can fall back to HTML).
    """
    html, error = fetch_url(PRODUCT_URL + ".js")
    if error or not html:
        return None

    try:
        data = json.loads(html)
    except Exception:
        return None

    variants = data.get("variants") or []
    if not variants:
        return None

    if TARGET_VARIANT_ID is not None:
        target = next(
            (v for v in variants if str(v.get("id")) == str(TARGET_VARIANT_ID)),
            None,
        )
        if target is None:
            # Variant id no longer exists (product may have changed).
            return "UNKNOWN", "target variant id not found in product JSON"
        colour = target.get("title", "variant")
        if target.get("available"):
            return "IN_STOCK", f"Shopify reports '{colour}' available"
        return "OUT_OF_STOCK", f"Shopify reports '{colour}' not available"

    # No specific variant: in stock if ANY variant is available.
    available = [v.get("title", "variant") for v in variants if v.get("available")]
    if available:
        return "IN_STOCK", "Shopify reports available: " + ", ".join(available)
    return "OUT_OF_STOCK", "Shopify reports all variants unavailable"


def check_via_html():
    """
    Fallback method: read the product page and use schema.org availability,
    which is more reliable than the visible button text on this page.
    """
    html, error = fetch_url(PRODUCT_URL)
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
    if "add to cart" in text or "add to basket" in text:
        return "POSSIBLE_IN_STOCK", "page shows an add-to-cart button"

    return "UNKNOWN", "could not determine stock from page"


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


# -----------------------------
# Main check
# -----------------------------

def main():
    seen = load_seen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    watch_url = PRODUCT_URL
    if TARGET_VARIANT_ID is not None:
        watch_url = f"{PRODUCT_URL}?variant={TARGET_VARIANT_ID}"

    print(f"\nMeaco Arete stock check: {now}")
    print(f"Watching: {PRODUCT_NAME}")
    print(f"  {watch_url}")
    print("-" * 70)

    result = check_via_shopify_json()
    method = "Shopify JSON"
    if result is None:
        result = check_via_html()
        method = "HTML fallback"

    status, reason = result
    print(f"[{status}] ({method}) {reason}")

    in_stock = status in ("IN_STOCK", "POSSIBLE_IN_STOCK")

    if not in_stock:
        print("\nNot in stock yet.")
        return

    print("\nIN STOCK!")

    # Only notify once per status until it changes.
    seen_key = f"{PRODUCT_HANDLE}|{TARGET_VARIANT_ID}|{status}"
    if not seen.get(seen_key):
        seen[seen_key] = {"first_seen": now, "status": status, "reason": reason}

        title = "Meaco Arete One 6L in stock!"
        message = f"{PRODUCT_NAME} is available. Buy now.\n{watch_url}"
        mac_notify(title, message)
        phone_notify(title, message, click_url=watch_url)
        open_in_browser(watch_url)

    save_seen(seen)


if __name__ == "__main__":
    main()

 

