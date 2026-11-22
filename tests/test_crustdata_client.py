"""Cache dates and expiry, offline mode, retries, credit limit and ledger, against a fake API."""

import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_db  # noqa: E402
import crustdata_client  # noqa: E402
import ledger  # noqa: E402
from crustdata_client import (  # noqa: E402
    BudgetExceeded, CrustdataClient, CrustdataError, NotInCache, SourceUnavailable,
)


def ok(data, credits="0.2"):
    return 200, {"x-credits-used": credits}, data


class FakeApi:
    """Stands in for CrustdataClient._send: returns the given replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, method, path, body):
        self.calls += 1
        reply = self.replies[min(self.calls, len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = agent_db.connect(":memory:")
        sleep = mock.patch.object(crustdata_client.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def make_client(self, api, **options):
        self.client = CrustdataClient(
            api_key="test", cache_path=Path(self.tmp.name) / "cache.sqlite3",
            run_id="run-1", ledger_db=self.db, **options,
        )
        self.client._send = api
        return self.client

    def ledger_statuses(self):
        return [row["status"] for row in self.db.execute("SELECT status FROM ledger ORDER BY id")]

    def test_cache_hit_keeps_the_original_retrieval_date(self):
        api = FakeApi(ok({"name": "Crustdata"}))
        client = self.make_client(api)

        first = client.call("/company/identify", {"domains": ["crustdata.com"]})
        second = client.call("/company/identify", {"domains": ["crustdata.com"]})

        self.assertEqual(api.calls, 1)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.retrieved_at, first.retrieved_at)
        self.assertEqual(second.data, {"name": "Crustdata"})
        self.assertEqual(self.ledger_statuses(), ["ok", "cached"])
        self.assertEqual(ledger.credits_spent(self.db, "run-1"), 0.2)

    def test_request_still_returns_plain_json(self):
        client = self.make_client(FakeApi(ok({"jobs": []})))
        self.assertEqual(client.request("/job/search", {"limit": 1}), {"jobs": []})

    def test_expired_cache_entry_is_fetched_again(self):
        api = FakeApi(ok({"v": 1}), ok({"v": 2}))
        client = self.make_client(api, ttl_hours={"default": 0})

        client.call("/job/search", {"limit": 1})
        self.assertEqual(client.call("/job/search", {"limit": 1}).data, {"v": 2})
        self.assertEqual(api.calls, 2)

    def test_offline_mode_never_calls_the_api(self):
        api = FakeApi(ok({}))
        client = self.make_client(api, offline=True)

        with self.assertRaises(NotInCache):
            client.call("/job/search", {"limit": 1})
        self.assertEqual(api.calls, 0)

    def test_server_errors_and_timeouts_are_retried(self):
        api = FakeApi((503, {}, "busy"), urllib.error.URLError("timed out"), ok({"done": True}))

        self.assertEqual(self.make_client(api).request("/web/search/live", {"query": "x"}), {"done": True})
        self.assertEqual(api.calls, 3)

    def test_source_that_keeps_failing_is_reported_not_treated_as_empty(self):
        api = FakeApi((503, {}, "busy"))

        with self.assertRaises(SourceUnavailable):
            self.make_client(api).call("/web/enrich/live", {"urls": ["https://example.com"]})
        self.assertEqual(api.calls, crustdata_client.MAX_RETRIES + 1)
        self.assertEqual(self.ledger_statuses(), ["error"])

    def test_refused_request_is_not_retried(self):
        api = FakeApi((403, {}, {"error": "not on your plan"}))

        with self.assertRaises(CrustdataError) as caught:
            self.make_client(api).call("/company/enrich", {"crustdata_company_ids": [1]})
        self.assertNotIsInstance(caught.exception, SourceUnavailable)
        self.assertEqual(api.calls, 1)

    def test_credit_limit_stops_new_calls(self):
        api = FakeApi(ok({}, credits="10"))
        client = self.make_client(api, credit_limit=10)
        client.call("/job/search", {"limit": 1})

        with self.assertRaises(BudgetExceeded):
            client.call("/job/search", {"limit": 2})
        self.assertEqual(api.calls, 1)


class RateLimiterTest(unittest.TestCase):
    def test_waits_once_the_minute_is_full(self):
        clock = [0.0]

        def fake_sleep(seconds):
            clock[0] += seconds

        limiter = crustdata_client.RateLimiter(2)
        with mock.patch.object(crustdata_client.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(crustdata_client.time, "sleep", side_effect=fake_sleep) as sleep:
            for _ in range(3):
                limiter.acquire()

        sleep.assert_called_once_with(60.0)

    def test_company_identify_is_held_to_five_a_minute(self):
        client = CrustdataClient(api_key="test", cache_path=":memory:")
        self.assertEqual(client._limiter_for("/company/identify").max_requests, 5)
        self.assertIsNone(client._limiter_for("/social_post/search"))
        client.close()


if __name__ == "__main__":
    unittest.main()
