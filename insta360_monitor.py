#!/usr/bin/env python3
"""Watch US availability of the Luna Ultra telephoto lens, SKU 7069."""

import argparse
import datetime as dt
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

from monitor import get_json, save_state

PRODUCT_NAME = "Ultra Telephoto Lens for Luna Ultra"
PRODUCT_SLUG = "ultra-telephoto-lens-for-luna-ultra"
PRODUCT_URL = f"https://store.insta360.com/product/{PRODUCT_SLUG}?c=7069"
SKU = "7069"
STATE_FILE = Path("state/insta360-lens.json")


class ProductHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.buttons = []
        self.script = None
        self.button = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self.script = {"attrs": attributes, "text": ""}
        if tag == "button" and attributes.get("id") == "product_submit_btn":
            self.button = {"disabled": "disabled" in attributes, "text": ""}

    def handle_data(self, text):
        if self.script is not None:
            self.script["text"] += text
        if self.button is not None:
            self.button["text"] += text

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append(self.script)
            self.script = None
        if tag == "button" and self.button is not None:
            self.buttons.append(self.button)
            self.button = None


def structured_products(value):
    if isinstance(value, dict):
        kind = value.get("@type", [])
        if kind == "Product" or isinstance(kind, list) and "Product" in kind:
            yield value
        for child in value.values():
            yield from structured_products(child)
    elif isinstance(value, list):
        for child in value:
            yield from structured_products(child)


def parse_availability(html):
    """Require US region, exact variant, product flags, offer, and buy button."""
    page = ProductHTML()
    page.feed(html)
    try:
        next_scripts = [s for s in page.scripts if s["attrs"].get("id") == "__NEXT_DATA__"]
        if len(next_scripts) != 1:
            raise ValueError("missing or duplicate product state")
        data = json.loads(next_scripts[0]["text"])["props"]
        country = data["pageData"]["runtimeInject"]["country"]
        if country.get("code") != "US" or country.get("currency") != "USD":
            raise ValueError("page is not the United States store")
        product_state = data["pageProps"]["initialState"]["product"]
        detail = product_state["productDetail"]
        if (str(detail.get("id")) != "923" or detail.get("url_key") != PRODUCT_SLUG
                or str(product_state.get("curSelectCommodityId")) != SKU):
            raise ValueError("page or selected option is not the requested lens")
        variants = [p for p in detail["commodities"] if str(p.get("id")) == SKU]
        if len(variants) != 1:
            raise ValueError("requested lens variant is missing or ambiguous")
        item = variants[0]
        if (detail.get("enabled") is not True or item.get("enabled") is not True
                or item.get("normal_sale") is not True or item["price"].get("currency") != "USD"):
            raise ValueError("product is not a normal US sale listing")
        offers = []
        for script in page.scripts:
            if script["attrs"].get("type") != "application/ld+json":
                continue
            for product in structured_products(json.loads(script["text"])):
                if str(product.get("sku")) == SKU:
                    entries = product.get("offers", [])
                    offers.extend(entries if isinstance(entries, list) else [entries])
        if not offers or any(o.get("priceCurrency") != "USD" for o in offers):
            raise ValueError("US offer details are missing")
        availability = {str(o.get("availability", "")).rstrip("/").rsplit("/", 1)[-1]
                        for o in offers}
        buttons = {" ".join(b["text"].lower().split()) for b in page.buttons}
        stock = item.get("stock")
        sale_state = item.get("sale_state")
        if type(stock) not in (int, float) or type(sale_state) is not int:
            raise ValueError("product stock flags are missing")
        if (sale_state == 0 and stock > 0 and item.get("isOutOfStock") is False
                and availability == {"InStock"} and buttons == {"add to cart"}
                and all(not button["disabled"] for button in page.buttons)):
            available = True
        elif (item.get("isOutOfStock") is True and availability == {"OutOfStock"}
                and buttons == {"notify me"}):
            available = False
        else:
            raise ValueError("stock flags, offer, and purchase button do not agree")
        price = item["price"].get("formatPrice")
        if not isinstance(price, str) or not price.strip():
            raise ValueError("formatted US price is missing")
        return {"available": available, "price": price.strip()[:64], "country": "US", "sku": SKU}
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"Could not confirm Insta360 availability: {exc}; previous state preserved") from None


def fetch_availability():
    for attempt in range(3):
        request = urllib.request.Request(PRODUCT_URL, headers={
            "User-Agent": "Mozilla/5.0 (compatible; StockMonitor/1.0)",
            "Cookie": "X_Country_Code=US; X_Currency=USD; X_Language=en_us",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return parse_availability(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Insta360 returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("Insta360 request failed; previous state preserved") from None


def load_state(path):
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if (not isinstance(state, dict) or state.get("version") != 1
            or state.get("sku") != SKU or state.get("country") != "US"
            or type(state.get("available")) is not bool or not isinstance(state.get("pending"), list)):
        raise RuntimeError("Invalid saved lens state; restore it instead of reinitializing")
    for event in state["pending"]:
        if (not isinstance(event, dict) or event.get("sku") != SKU
                or event.get("country") != "US" or not isinstance(event.get("price"), str)):
            raise RuntimeError("Invalid pending lens alert; previous state preserved")
    return state


def send_discord(webhook, event):
    # Reuse the same safe HTTP sender, but keep the destination and product link fixed.
    from urllib.parse import urlsplit, urlunsplit
    url = urlsplit(webhook)
    if (url.scheme != "https" or url.hostname not in {"discord.com", "discordapp.com"}
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", url.path)):
        raise RuntimeError("DISCORD_WEBHOOK_URL is not a Discord webhook URL")
    destination = urlunsplit(("https", url.netloc, url.path, "wait=true", ""))
    get_json(destination, data={
        "username": "Insta360 Stock Alerts",
        "allowed_mentions": {"parse": []},
        "embeds": [{"title": f"Back in stock: {PRODUCT_NAME}",
                    "description": "The United States store showed Add to cart when this stock was detected.",
                    "url": PRODUCT_URL, "color": 3066993,
                    "fields": [{"name": "Price", "value": event["price"], "inline": True},
                               {"name": "Store", "value": "United States", "inline": True}]}],
    })


def run_monitor(path=STATE_FILE, *, webhook="", git_persist=False,
                scan=fetch_availability, sender=send_discord, today=None):
    state = load_state(path)
    status = scan()
    if (not isinstance(status, dict) or status.get("country") != "US" or status.get("sku") != SKU
            or type(status.get("available")) is not bool or not isinstance(status.get("price"), str)):
        raise RuntimeError("Unrecognized lens availability; previous state preserved")
    day = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    if state is None:
        state = {"version": 1, "sku": SKU, "country": "US", "available": status["available"],
                 "baseline_date": day, "last_success_date": day, "pending": []}
        save_state(path, state, git_persist=git_persist)
        print(f"Lens baseline saved: {'in stock' if status['available'] else 'out of stock'} (US); no alerts sent.")
        return
    changed = state["available"] != status["available"] or state.get("last_success_date") != day
    if status["available"] and not state["available"]:
        state["pending"].append({**status, "id": str(uuid.uuid4()), "detected_on": day})
    state["available"] = status["available"]
    state["last_success_date"] = day
    if changed:
        save_state(path, state, git_persist=git_persist)
    if state["pending"] and not webhook:
        raise RuntimeError("Set DISCORD_WEBHOOK_URL to deliver pending lens alerts")
    delivered = 0
    while state["pending"]:
        sender(webhook, state["pending"][0])
        state["pending"].pop(0)
        save_state(path, state, git_persist=git_persist)
        delivered += 1
        time.sleep(0.5)
    print(f"Lens checked: {'in stock' if status['available'] else 'out of stock'} (US), "
          f"{status['price']}; delivered {delivered} stock alerts.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--git-persist", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.dry_run:
            print(json.dumps(fetch_availability(), sort_keys=True))
            return
        run_monitor(args.state, webhook=os.environ.get("DISCORD_WEBHOOK_URL", ""), git_persist=args.git_persist)
    except Exception as exc:
        print(f"Lens monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
