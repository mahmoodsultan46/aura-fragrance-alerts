#!/usr/bin/env python3
"""Watch Aura's men's new arrivals; Python standard library only."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

COLLECTION = "https://www.aurafragrance.com/collections/mens-new-arrivals"
STATE_FILE = Path("state/seen.json")
PAGE_SIZE = 250
MAX_PAGES = 60


def get_json(url, *, data=None):
    """Retry safe GETs/rate limits; never log the secret webhook URL."""
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"User-Agent": "AuraNewArrivalMonitor/1.0", "Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            # Discord 429 explicitly means the message was not accepted.
            if exc.code == 429 and attempt < 2:
                try:
                    delay = float(json.loads(exc.read()).get("retry_after", 5))
                except (ValueError, TypeError):
                    delay = 5
                time.sleep(min(max(delay, 1), 30))
                continue
            if data is None and exc.code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{'Discord' if data is not None else 'Aura'} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if data is None and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{'Discord' if data is not None else 'Aura'} request failed; will retry next run") from None


def scan_collection(fetch=get_json):
    """Read every collection page. Incomplete/malformed scans cannot notify."""
    result = {}
    previous_page = None
    for page in range(1, MAX_PAGES + 1):
        payload = fetch(f"{COLLECTION}/products.json?limit={PAGE_SIZE}&page={page}")
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise RuntimeError("Aura feed changed format; previous state is preserved")
        items = payload["products"]
        ids = []
        for product in items:
            if not isinstance(product, dict):
                raise RuntimeError("Invalid product in Aura feed")
            pid = str(product.get("id", ""))
            title, handle = product.get("title"), product.get("handle")
            if not pid.isdigit() or not isinstance(title, str) or not title.strip():
                raise RuntimeError("Invalid product identity in Aura feed")
            if not isinstance(handle, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", handle):
                raise RuntimeError("Invalid product link in Aura feed")
            ids.append(pid)
            result[pid] = {"id": pid, "title": title.strip(), "handle": handle}
        if ids and ids == previous_page:
            raise RuntimeError("Aura repeated a pagination page; previous state is preserved")
        previous_page = ids
        if len(items) < PAGE_SIZE:
            if not result:
                raise RuntimeError("Aura returned an empty collection; previous state is preserved")
            return result
        time.sleep(0.1)
    raise RuntimeError("Aura exceeded the page safety limit; previous state is preserved")


def load_state(path):
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("seen"), list)
            or not data["seen"] or not all(isinstance(x, str) and x.isdigit() for x in data["seen"])
            or not isinstance(data.get("pending"), list)):
        raise RuntimeError("Invalid saved state; restore it instead of reinitializing")
    return data


def save_state(path, state, *, git_persist=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    if not git_persist:
        return
    subprocess.run(["git", "add", "--", str(path)], check=True)
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode
    if changed == 0:
        return
    if changed != 1:
        raise RuntimeError("Could not inspect state changes")
    subprocess.run(["git", "commit", "-m", "Update monitor product history [skip ci]"], check=True)
    # Persist the outbox before any Discord send. A failed push stops this run.
    subprocess.run(["git", "push", "origin", "HEAD"], check=True)


def send_discord(webhook, product):
    url = urllib.parse.urlsplit(webhook)
    if (url.scheme != "https" or url.hostname not in {"discord.com", "discordapp.com"}
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", url.path)):
        raise RuntimeError("DISCORD_WEBHOOK_URL is not a Discord webhook URL")
    destination = urllib.parse.urlunsplit(("https", url.netloc, url.path, "wait=true", ""))
    get_json(destination, data={
        "username": "Aura New Arrivals",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": product["title"][:256],
            "description": "Newly added to Aura Fragrance men's new arrivals.",
            "url": "https://www.aurafragrance.com/products/" + product["handle"],
            "color": 3447003,
        }],
    })


def run_monitor(path=STATE_FILE, *, webhook="", git_persist=False,
                scan=scan_collection, sender=send_discord, today=None):
    state = load_state(path)
    current = scan()
    day = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    if state is None:
        # Two agreeing complete scans avoid a baseline during pagination changes.
        confirmation = scan()
        if set(current) != set(confirmation):
            raise RuntimeError("Collection changed during initialization; try again next run")
        state = {"version": 1, "seen": sorted(current), "pending": [],
                 "baseline_date": day, "last_success_date": day}
        save_state(path, state, git_persist=git_persist)
        print(f"Baseline saved: {len(current)} existing products; no alerts sent.")
        return

    seen = set(state["seen"])
    added = [p for pid, p in current.items() if pid not in seen]
    changed = bool(added) or state.get("last_success_date") != day
    if added:
        state["pending"].extend(added)
        state["seen"] = sorted(seen | set(current))
    state["last_success_date"] = day
    if changed:
        save_state(path, state, git_persist=git_persist)
    if state["pending"] and not webhook:
        raise RuntimeError("Set the DISCORD_WEBHOOK_URL Actions secret to deliver pending alerts")

    delivered = 0
    while state["pending"]:
        product = state["pending"][0]
        sender(webhook, product)
        state["pending"].pop(0)
        save_state(path, state, git_persist=git_persist)
        delivered += 1
        time.sleep(0.5)
    print(f"Checked {len(current)} products; delivered {delivered} new-product alerts.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--git-persist", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Read the live collection without saving or sending")
    args = parser.parse_args()
    try:
        if args.dry_run:
            products = scan_collection()
            print(f"Successfully read {len(products)} products across all collection pages.")
            return
        run_monitor(args.state, webhook=os.environ.get("DISCORD_WEBHOOK_URL", ""),
                    git_persist=args.git_persist)
    except Exception as exc:
        # Avoid tracebacks which may include a secret destination URL.
        print(f"Monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
