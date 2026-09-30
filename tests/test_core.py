from support import fixture_config
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from ticket_bot import Client, MonitorError, NoRedirect, State, VisualGate, SoundGate, parse_page, validate_request, retry_seconds


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.state = State(self.path, "TestUser")

    def test_quiet_baseline_then_addition_with_existing_tickets(self):
        self.assertEqual(self.state.accept({100, 101}), set())
        self.assertEqual(self.state.accept({100, 101, 102}), {102})
        self.assertEqual(self.state.pending, 1)

    def test_older_ticket_entering_queue(self):
        self.state.accept({100})
        self.assertEqual(self.state.accept({4, 100}), {4})

    def test_empty_baseline_then_ticket(self):
        self.state.accept(set())
        self.assertTrue(self.state.initialized)
        self.assertEqual(self.state.accept({1}), {1})

    def test_empty_after_baseline_does_not_forget_ids(self):
        self.state.accept({1})
        self.state.accept(set())
        self.assertEqual(self.state.accept({1, 2}), {2})

    def test_restart_dedup_and_deferred_count(self):
        self.state.accept({10})
        self.state.accept({1, 10, 11})
        restored = State(self.path, "TestUser")
        self.assertEqual(restored.accept({1, 10, 11}), set())
        self.assertEqual(restored.pending, 2)
        restored.acknowledge_visuals(2)
        self.assertEqual(State(self.path, "TestUser").pending, 0)

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("bad", encoding="utf-8")
        with self.assertRaises(MonitorError):
            State(self.path, "TestUser")

    def test_failed_write_does_not_change_memory(self):
        with patch("ticket_bot.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.state.accept({1})
        self.assertFalse(self.state.initialized)
        self.assertEqual(self.state.seen, set())


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.config = {"username": "TestUser", "page_size": 100, "request_timeout_seconds": 2, "max_pages": 10}
        self.stop = Mock()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False
        self.opener = Mock()
        self.client = Client(self.config, "unit-test-value-not-a-real-key", self.stop, self.opener)

    def response(self, data):
        result = Mock()
        result.__enter__ = Mock(return_value=result)
        result.__exit__ = Mock(return_value=False)
        result.status = 200
        result.read.return_value = json.dumps(data).encode()
        return result

    def test_requests_are_get_only_to_exact_queue(self):
        self.opener.open.return_value = self.response([])
        self.client.page(None)
        self.client.page(42)
        for call in self.opener.open.call_args_list:
            request = call.args[0]
            validate_request(request)
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertTrue(request.full_url.startswith("https://e621.net/tickets.json?"))
            self.assertNotIn("unit-test-value", request.full_url)

    def test_unsafe_requests_blocked(self):
        good = "https://e621.net/tickets.json?search%5Bstatus%5D=pending_unclaimed&limit=100"
        bad_urls = [good.replace("https:", "http:"), good.replace("e621.net", "evil.test"),
                    good.replace("e621.net", "e621.net:443"), good.replace("tickets.json", "tickets/1/claim.json"),
                    good + "&api_key=x", good + "&limit=1", good + "#fragment", good + "&page=1",
                    good.replace("pending_unclaimed", "pending"), good.replace("e621.net", "user@e621.net")]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(MonitorError):
                validate_request(urllib.request.Request(url))
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method=method), self.assertRaises(MonitorError):
                validate_request(urllib.request.Request(good, method=method))
        with self.assertRaises(MonitorError):
            validate_request(urllib.request.Request(good, data=b"x", method="GET"))

    def test_redirects_never_followed(self):
        handler = NoRedirect()
        req = urllib.request.Request("https://e621.net/tickets.json", headers={"Authorization": "secret"})
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.assertIsNone(handler.redirect_request(req, None, code, "redirect", {}, "https://evil.test/"))
        self.assertTrue(any(isinstance(h, NoRedirect) for h in Client(self.config, "test", self.stop).opener.handlers))

    def test_placeholder_rejected_before_any_network(self):
        with self.assertRaises(MonitorError):
            Client(self.config, "KEYPLACEHOLDER", self.stop, self.opener)
        self.opener.open.assert_not_called()

    def test_pagination_includes_older_ids_after_short_page(self):
        self.opener.open.side_effect = [self.response([{"id": 100, "status": "pending"}]),
                                        self.response([{"id": 2, "status": "pending"}]), self.response([])]
        self.assertEqual(self.client.snapshot(), {2: None, 100: None})
        self.assertIn("page=b100", self.opener.open.call_args_list[1].args[0].full_url)

    def test_later_page_failure_does_not_accept_partial_scan(self):
        self.opener.open.side_effect = [self.response([{"id": 100, "status": "pending"}]),
                                        urllib.error.URLError("test")]
        with self.assertRaises(MonitorError):
            self.client.snapshot()

    def test_http_errors_are_not_empty_queues(self):
        for code in (401, 403, 429, 500):
            self.opener.open.side_effect = urllib.error.HTTPError("https://e621.net/tickets.json", code, "private body", {"Retry-After": "120"}, None)
            with self.subTest(code=code), self.assertRaises(MonitorError) as result:
                self.client.snapshot()
            self.assertEqual(result.exception.retry_after, 120)
            self.assertNotIn("private body", str(result.exception))

    def test_empty_success_and_malformed_responses(self):
        self.assertEqual(parse_page([]), {})
        for value in ({"error": "denied"}, [{"id": True}], [{"id": 1, "status": "approved"}],
                      [{"id": 1, "status": "pending", "claimant_id": 3}], [{"id": "3"}],
                      [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}]):
            with self.subTest(value=value), self.assertRaises(MonitorError):
                parse_page(value)

    def test_repeated_page_is_error(self):
        self.opener.open.return_value = self.response([{"id": 1, "status": "pending"}])
        with self.assertRaises(MonitorError):
            self.client.snapshot()

    def test_invalid_retry_after(self):
        self.assertEqual(retry_seconds("nonsense"), 0)
        self.assertEqual(retry_seconds("-1"), 0)


class GateTests(unittest.TestCase):
    def test_sound_bursts_coalesce_without_backlog(self):
        gate = SoundGate()
        self.assertTrue(gate.ready(100, 3))
        for now in (100, 100.5, 101, 102.99):
            self.assertFalse(gate.ready(now, 3))
        self.assertTrue(gate.ready(103, 3))

    def test_deferred_combined_visuals_wait_for_stable_desktop(self):
        gate = VisualGate()
        for count in (1, 2, 5):
            self.assertFalse(gate.ready(True, count))
        self.assertFalse(gate.ready(False, 5))
        self.assertTrue(gate.ready(False, 5))
        self.assertFalse(gate.ready(True, 5))
        self.assertFalse(gate.ready(False, 5))

    def test_no_notification_for_zero_count(self):
        gate = VisualGate()
        for _ in range(10):
            self.assertFalse(gate.ready(False, 0))


if __name__ == "__main__":
    unittest.main()
