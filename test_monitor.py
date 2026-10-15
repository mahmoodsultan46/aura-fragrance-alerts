"""Behavioral checks for new-only alerts and durable delivery state."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import monitor


def product(pid, title=None, **extra):
    pid = str(pid)
    return {"id": pid, "title": title or "Fragrance " + pid,
            "handle": "fragrance-" + pid, **extra}


def catalog(*products):
    return {str(item["id"]): item for item in products}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "state" / "seen.json"
        self.sleep = patch.object(monitor.time, "sleep").start()
        self.addCleanup(patch.stopall)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def run_monitor(self, current, sender=None, **options):
        monitor.run_monitor(self.path, scan=lambda: current,
                            sender=sender if sender is not None else Mock(),
                            today=options.pop("today", "2026-10-15"), **options)

    def initialize(self, current):
        self.run_monitor(current)

    def state(self):
        return monitor.load_state(self.path)

    def test_initial_catalog_is_a_silent_baseline(self):
        current = catalog(product(1), product(2))
        scan = Mock(side_effect=[current, dict(reversed(list(current.items())))])
        sender = Mock()
        monitor.run_monitor(self.path, scan=scan, sender=sender, today="2026-10-15")
        self.assertEqual(scan.call_count, 2)
        sender.assert_not_called()
        self.assertEqual(set(self.state()["seen"]), {"1", "2"})
        self.assertEqual(self.state()["pending"], [])
        self.assertEqual(self.state()["baseline_date"], "2026-10-15")

    def test_unstable_baseline_does_not_create_state_or_send(self):
        sender = Mock()
        scan = Mock(side_effect=[catalog(product(1)), catalog(product(1), product(2))])
        with self.assertRaises(RuntimeError):
            monitor.run_monitor(self.path, scan=scan, sender=sender)
        self.assertFalse(self.path.exists())
        sender.assert_not_called()

    def test_only_new_id_is_sent_and_is_persisted_before_delivery(self):
        self.initialize(catalog(product(1)))
        checks = []

        def sender(webhook, new_product):
            saved = self.state()
            checks.append((webhook, new_product["id"]))
            self.assertIn("2", saved["seen"])
            self.assertEqual([p["id"] for p in saved["pending"]], ["2"])

        self.run_monitor(catalog(product(1), product(2)), sender, webhook="configured")
        self.assertEqual(checks, [("configured", "2")])
        self.assertEqual(self.state()["pending"], [])
        repeat = Mock()
        self.run_monitor(catalog(product(1), product(2)), repeat, webhook="configured")
        repeat.assert_not_called()

    def test_price_stock_title_and_order_changes_do_not_notify(self):
        self.initialize(catalog(product(1), product(2)))
        sender = Mock()
        self.run_monitor(catalog(product(2, title="Renamed", price="9.99", available=True),
                                 product(1, price="99.00", available=False)),
                         sender, webhook="configured")
        sender.assert_not_called()
        self.assertEqual(self.state()["pending"], [])

    def test_removed_and_readded_product_remains_seen(self):
        self.initialize(catalog(product(1), product(2)))
        sender = Mock()
        self.run_monitor(catalog(product(1)), sender, webhook="configured")
        self.run_monitor(catalog(product(2), product(1)), sender, webhook="configured")
        sender.assert_not_called()
        self.assertEqual(set(self.state()["seen"]), {"1", "2"})

    def test_partial_delivery_retries_only_pending_even_if_product_disappears(self):
        self.initialize(catalog(product(1)))
        attempted = []

        def partial_sender(webhook, new_product):
            attempted.append(new_product["id"])
            if new_product["id"] == "3":
                raise RuntimeError("delivery failed")

        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            self.run_monitor(catalog(product(1), product(2), product(3)),
                             partial_sender, webhook="configured")
        self.assertEqual(attempted, ["2", "3"])
        self.assertEqual(set(self.state()["seen"]), {"1", "2", "3"})
        self.assertEqual([p["id"] for p in self.state()["pending"]], ["3"])
        retry = Mock()
        self.run_monitor(catalog(product(1)), retry, webhook="configured")
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.args[1]["id"], "3")
        self.assertEqual(self.state()["pending"], [])

    def test_missing_webhook_preserves_pending_for_later_delivery(self):
        self.initialize(catalog(product(1)))
        sender = Mock()
        with self.assertRaisesRegex(RuntimeError, "DISCORD_WEBHOOK_URL"):
            self.run_monitor(catalog(product(1), product(2)), sender)
        sender.assert_not_called()
        self.assertEqual([p["id"] for p in self.state()["pending"]], ["2"])
        self.run_monitor(catalog(product(1), product(2)), sender, webhook="configured")
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.state()["pending"], [])

    def test_failed_scan_preserves_exact_state_and_does_not_send(self):
        self.initialize(catalog(product(1)))
        before = self.path.read_bytes()
        sender = Mock()

        def broken_scan():
            with patch.object(monitor, "PAGE_SIZE", 1):
                pages = iter([{"products": [product(1)]}, {"unexpected": []}])
                return monitor.scan_collection(fetch=lambda url: next(pages))

        with self.assertRaises(RuntimeError):
            monitor.run_monitor(self.path, scan=broken_scan, sender=sender,
                                webhook="configured", today="2026-10-16")
        self.assertEqual(self.path.read_bytes(), before)
        sender.assert_not_called()

    def test_daily_keepalive_updates_history_without_notification(self):
        self.initialize(catalog(product(1)))
        sender = Mock()
        self.run_monitor(catalog(product(1)), sender, today="2026-10-16")
        sender.assert_not_called()
        self.assertEqual(self.state()["last_success_date"], "2026-10-16")
        self.assertEqual(self.state()["baseline_date"], "2026-10-15")

    def test_corrupt_history_never_reinitializes_or_scans(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 1, "seen": [], "pending": []}))
        before = self.path.read_bytes()
        scan, sender = Mock(), Mock()
        with self.assertRaises(RuntimeError):
            monitor.run_monitor(self.path, scan=scan, sender=sender)
        scan.assert_not_called()
        sender.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)


class CollectionScanTests(unittest.TestCase):
    def setUp(self):
        patch.object(monitor, "PAGE_SIZE", 2).start()
        patch.object(monitor.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def test_full_pagination_includes_products_beyond_first_page(self):
        urls = []
        pages = {1: [product(1), product(2)], 2: [product(3)]}

        def fetch(url):
            urls.append(url)
            parameters = parse_qs(urlsplit(url).query)
            self.assertEqual(parameters["limit"], ["2"])
            return {"products": pages[int(parameters["page"][0])]}

        result = monitor.scan_collection(fetch=fetch)
        self.assertEqual(set(result), {"1", "2", "3"})
        self.assertEqual(len(urls), 2)

    def test_exact_page_boundary_requires_terminal_empty_page(self):
        fetch = Mock(side_effect=[{"products": [product(1), product(2)]}, {"products": []}])
        self.assertEqual(set(monitor.scan_collection(fetch=fetch)), {"1", "2"})
        self.assertEqual(fetch.call_count, 2)

    def test_repeated_page_is_rejected_instead_of_accepted_as_complete(self):
        page = {"products": [product(1), product(2)]}
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            monitor.scan_collection(fetch=Mock(side_effect=[page, page]))

    def test_empty_collection_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "empty"):
            monitor.scan_collection(fetch=lambda url: {"products": []})

    def test_malformed_identity_or_external_link_is_rejected(self):
        malformed = [None, [], {"products": {}}, {"products": [None]},
                     {"products": [product("not-numeric")]},
                     {"products": [product(1, title=" ")]},
                     {"products": [product(1, handle="https://other.example/path")]},
                     {"products": [product(1, handle="../outside")]},
                     {"products": [product(1, title=None)]}]
        # product(..., title=None) intentionally uses its default; replace explicitly.
        malformed[-1]["products"][0]["title"] = None
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError):
                    monitor.scan_collection(fetch=lambda url, value=payload: value)

    def test_unterminated_feed_hits_safety_limit(self):
        counter = iter(range(1, 10))

        def fetch(url):
            n = next(counter) * 2
            return {"products": [product(n), product(n + 1)]}

        with patch.object(monitor, "MAX_PAGES", 2):
            with self.assertRaisesRegex(RuntimeError, "safety limit"):
                monitor.scan_collection(fetch=fetch)


if __name__ == "__main__":
    unittest.main()
