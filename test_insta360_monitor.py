"""Behavioral checks for US stock detection and durable Discord delivery."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import insta360_monitor as monitor


def stock_status(available=False, price="$49.99", **changes):
    return {"available": available, "price": price,
            "country": "US", "sku": "7069", **changes}


def fixture_data(available=False):
    """The store's sale flags are authoritative; stock alone is not."""
    return {
        "props": {
            "pageData": {"runtimeInject": {"country": {
                "code": "US", "currency": "USD"}}},
            "pageProps": {"initialState": {"product": {
                "curSelectCommodityId": 7069,
                "productDetail": {
                    "id": 923,
                    "url_key": "ultra-telephoto-lens-for-luna-ultra",
                    "enabled": True,
                    "commodities": [{
                        "id": 7069,
                        "enabled": True,
                        "normal_sale": True,
                        "sale_state": 0 if available else -1,
                        "stock": 783 if available else 37,
                        "isOutOfStock": not available,
                        "price": {"currency": "USD", "formatPrice": "$49.99"},
                    }],
                },
            }}},
        },
    }


def fixture_html(available=False, *, data=None, ld=None, button=None):
    if data is None:
        data = fixture_data(available)
    if ld is None:
        ld = {"@context": "https://schema.org", "@type": "Product",
              "sku": "7069", "offers": [{"priceCurrency": "USD",
              "availability": "InStock" if available else "OutOfStock"}]}
    if button is None:
        button = ('<button id="product_submit_btn">Add to cart</button>' if available
                  else '<button id="product_submit_btn">Notify me</button>')
    return ('<!doctype html><html><body>'
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + '</script>'
            '<script type="application/ld+json">' + json.dumps(ld) + '</script>'
            + button + '</body></html>')


class AvailabilityParsingTests(unittest.TestCase):
    def assert_unknown(self, html):
        with self.assertRaises(RuntimeError):
            monitor.parse_availability(html)

    def test_us_out_of_stock_remains_unavailable_despite_positive_stock(self):
        result = monitor.parse_availability(fixture_html(False))
        self.assertFalse(result["available"])
        self.assertEqual(result["country"], "US")
        self.assertEqual(str(result["sku"]), "7069")
        self.assertEqual(result["price"], "$49.99")

    def test_consistent_us_in_stock_markers_are_available(self):
        result = monitor.parse_availability(fixture_html(True))
        self.assertTrue(result["available"])
        self.assertEqual(result["price"], "$49.99")

    def test_schema_availability_accepts_prefixed_and_unqualified_values(self):
        for available in (False, True):
            for prefix in ("", "https://schema.org/"):
                ld = {"@type": "Product", "sku": "7069", "offers": [{
                    "priceCurrency": "USD",
                    "availability": prefix + ("InStock" if available else "OutOfStock"),
                }]}
                with self.subTest(available=available, prefix=prefix):
                    self.assertIs(monitor.parse_availability(
                        fixture_html(available, ld=ld))["available"], available)

    def test_other_country_is_rejected_even_when_same_sku_is_available(self):
        data = fixture_data(True)
        data["props"]["pageData"]["runtimeInject"]["country"]["code"] = "GB"
        self.assert_unknown(fixture_html(True, data=data))

    def test_non_usd_region_or_price_is_rejected(self):
        for change in ("region", "commodity", "schema"):
            data = fixture_data(True)
            ld = {"@type": "Product", "sku": "7069", "offers": [{
                "priceCurrency": "USD", "availability": "InStock"}]}
            if change == "region":
                data["props"]["pageData"]["runtimeInject"]["country"]["currency"] = "GBP"
            elif change == "commodity":
                data["props"]["pageProps"]["initialState"]["product"][
                    "productDetail"]["commodities"][0]["price"]["currency"] = "GBP"
            else:
                ld["offers"][0]["priceCurrency"] = "GBP"
            with self.subTest(change=change):
                self.assert_unknown(fixture_html(True, data=data, ld=ld))

    def test_wrong_product_or_sku_is_rejected(self):
        mutations = (
            lambda p: p["productDetail"].update(id=924),
            lambda p: p["productDetail"].update(url_key="another-lens"),
            lambda p: p["productDetail"]["commodities"][0].update(id=7070),
            lambda p: p.update(curSelectCommodityId=7070),
        )
        for mutate in mutations:
            data = fixture_data(True)
            mutate(data["props"]["pageProps"]["initialState"]["product"])
            self.assert_unknown(fixture_html(True, data=data))
        self.assert_unknown(fixture_html(True, ld={"@type": "Product", "sku": "7070",
            "offers": [{"priceCurrency": "USD", "availability": "InStock"}]}))

    def test_conflicting_sale_stock_or_schema_flags_never_report_available(self):
        changes = ({"isOutOfStock": True}, {"sale_state": -1},
                   {"stock": 0}, {"enabled": False}, {"normal_sale": False})
        for change in changes:
            data = fixture_data(True)
            data["props"]["pageProps"]["initialState"]["product"][
                "productDetail"]["commodities"][0].update(change)
            with self.subTest(change=change):
                self.assert_unknown(fixture_html(True, data=data))
        self.assert_unknown(fixture_html(True, ld={"@type": "Product", "sku": "7069",
            "offers": [{"priceCurrency": "USD", "availability": "OutOfStock"}]}))

    def test_disabled_or_wrong_purchase_button_is_rejected(self):
        for button in ('<button id="product_submit_btn" disabled>Add to cart</button>',
                       '<button id="product_submit_btn" disabled="false">Add to cart</button>',
                       '<button id="product_submit_btn">Notify me</button>',
                       '<button id="different_button">Add to cart</button>'):
            with self.subTest(button=button):
                self.assert_unknown(fixture_html(True, button=button))
        self.assert_unknown(fixture_html(False,
            button='<button id="product_submit_btn">Add to cart</button>'))

    def test_missing_or_malformed_markers_are_rejected(self):
        for html in ("", "<html>Temporarily unavailable</html>",
                     '<script id="__NEXT_DATA__">not json</script>',
                     fixture_html(True, data={}),
                     fixture_html(True, ld={}),
                     fixture_html(True, button="")):
            with self.subTest(html=html[:80]):
                self.assert_unknown(html)


class StockTransitionTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "state" / "insta360.json"
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def run_monitor(self, status, sender=None, **options):
        return monitor.run_monitor(self.path, scan=lambda: copy.deepcopy(status),
            sender=sender if sender is not None else Mock(),
            today=options.pop("today", "2026-10-15"), **options)

    def state(self):
        return json.loads(self.path.read_text())

    def test_first_observation_is_silent_for_either_stock_state(self):
        for available in (False, True):
            with self.subTest(available=available):
                path = self.path.with_name(str(available) + ".json")
                sender = Mock()
                monitor.run_monitor(path, scan=lambda: stock_status(available),
                    sender=sender, webhook="configured", today="2026-10-15")
                sender.assert_not_called()
                saved = json.loads(path.read_text())
                self.assertEqual(saved["available"], available)
                self.assertEqual(saved["pending"], [])
                self.assertEqual(saved["baseline_date"], "2026-10-15")
                self.assertEqual(saved["last_success_date"], "2026-10-15")

    def test_stock_transition_is_durable_before_discord_delivery(self):
        self.run_monitor(stock_status(False))
        events = []

        def sender(webhook, event):
            self.assertEqual(webhook, "configured")
            saved = self.state()
            self.assertTrue(saved["available"])
            self.assertEqual(saved["pending"], [event])
            self.assertEqual(event["price"], "$49.99")
            events.append(copy.deepcopy(event))

        self.run_monitor(stock_status(True), sender, webhook="configured")
        self.assertEqual(len(events), 1)
        self.assertEqual(self.state()["pending"], [])

    def test_repeated_stock_and_price_changes_are_silent(self):
        self.run_monitor(stock_status(False))
        sender = Mock()
        self.run_monitor(stock_status(False, "$45.00"), sender, webhook="configured")
        sender.assert_not_called()
        self.run_monitor(stock_status(True), sender, webhook="configured")
        self.run_monitor(stock_status(True, "$59.99"), sender, webhook="configured")
        self.run_monitor(stock_status(True, "$59.99"), sender, webhook="configured")
        self.assertEqual(sender.call_count, 1)

    def test_return_to_unavailable_rearms_next_restock(self):
        self.run_monitor(stock_status(False))
        sender = Mock()
        self.run_monitor(stock_status(True), sender, webhook="configured")
        self.run_monitor(stock_status(False), sender, webhook="configured")
        self.assertFalse(self.state()["available"])
        self.assertEqual(sender.call_count, 1)
        self.run_monitor(stock_status(True), sender, webhook="configured")
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(self.state()["pending"], [])

    def test_missing_webhook_retains_pending_for_later_delivery(self):
        self.run_monitor(stock_status(False))
        sender = Mock()
        with self.assertRaisesRegex(RuntimeError, "DISCORD_WEBHOOK_URL"):
            self.run_monitor(stock_status(True), sender)
        sender.assert_not_called()
        self.assertTrue(self.state()["available"])
        pending = copy.deepcopy(self.state()["pending"])
        self.assertEqual(len(pending), 1)
        self.run_monitor(stock_status(True), sender, webhook="configured")
        self.assertEqual(sender.call_args.args, ("configured", pending[0]))
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.state()["pending"], [])

    def test_delivery_failure_retries_original_pending_without_duplicate(self):
        self.run_monitor(stock_status(False))
        failed = Mock(side_effect=RuntimeError("delivery failed"))
        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            self.run_monitor(stock_status(True), failed, webhook="configured")
        pending = copy.deepcopy(self.state()["pending"])
        self.assertEqual(len(pending), 1)
        retry = Mock()
        self.run_monitor(stock_status(True, "$59.99"), retry, webhook="configured")
        self.assertEqual(retry.call_args.args, ("configured", pending[0]))
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(self.state()["pending"], [])

    def test_scan_failure_preserves_exact_history_and_does_not_deliver_pending(self):
        self.run_monitor(stock_status(False))
        with self.assertRaises(RuntimeError):
            self.run_monitor(stock_status(True), webhook="")
        before = self.path.read_bytes()
        scan = Mock(side_effect=RuntimeError("unknown stock state"))
        sender = Mock()
        with self.assertRaisesRegex(RuntimeError, "unknown stock state"):
            monitor.run_monitor(self.path, scan=scan, sender=sender,
                webhook="configured", today="2026-10-16")
        self.assertEqual(self.path.read_bytes(), before)
        sender.assert_not_called()

    def test_daily_success_updates_date_without_discord_message(self):
        self.run_monitor(stock_status(False))
        sender = Mock()
        self.run_monitor(stock_status(False), sender, today="2026-10-16")
        sender.assert_not_called()
        self.assertEqual(self.state()["last_success_date"], "2026-10-16")
        self.assertEqual(self.state()["baseline_date"], "2026-10-15")

    def test_corrupt_or_wrong_identity_state_never_resets_or_scans(self):
        self.run_monitor(stock_status(False))
        valid = self.state()
        corrupt = ("{not json", {**valid, "version": 99},
                   {**valid, "sku": "7070"}, {**valid, "country": "GB"},
                   {**valid, "available": "false"}, {**valid, "pending": {}},
                   {key: value for key, value in valid.items() if key != "available"})
        for state in corrupt:
            self.path.write_text(state if isinstance(state, str) else json.dumps(state))
            before = self.path.read_bytes()
            scan, sender = Mock(), Mock()
            with self.subTest(state=state):
                with self.assertRaises((RuntimeError, ValueError)):
                    monitor.run_monitor(self.path, scan=scan, sender=sender,
                        webhook="configured")
                scan.assert_not_called()
                sender.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
