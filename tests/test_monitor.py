import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import monitor
from monitor import (
    MonitorError,
    SmtpConfig,
    compare,
    get_json,
    has_changes,
    normalize_old_state,
    product_path,
    release_group_key,
    render_summary,
)


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def make_http_error(code):
    return HTTPError(url="https://example.com", code=code, msg="error", hdrs=None, fp=None)


class MonitorTests(unittest.TestCase):
    @patch("monitor.time.sleep")
    @patch("monitor.urlopen")
    def test_get_json_retries_on_transient_http_error(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [make_http_error(504), FakeResponse({"ok": True})]
        result = get_json("https://example.com/api", {})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("monitor.time.sleep")
    @patch("monitor.urlopen")
    def test_get_json_raises_after_exhausting_retries(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = make_http_error(504)
        with self.assertRaises(MonitorError):
            get_json("https://example.com/api", {})
        self.assertEqual(mock_urlopen.call_count, monitor.MAX_RETRIES + 1)

    @patch("monitor.time.sleep")
    @patch("monitor.urlopen")
    def test_get_json_does_not_retry_on_client_error(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = make_http_error(404)
        with self.assertRaises(MonitorError):
            get_json("https://example.com/api", {})
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("monitor.time.sleep")
    @patch("monitor.urlopen")
    def test_get_json_retries_on_url_error(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [URLError("timed out"), FakeResponse({"ok": True})]
        result = get_json("https://example.com/api", {})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_product_path_removes_support_suffix(self):
        url = "https://rog.asus.com/tw/motherboards/rog-strix/model/helpdesk_bios/"
        self.assertEqual(product_path(url), "tw/motherboards/rog-strix/model/")

    def test_detects_add_remove_and_change(self):
        old = {
            "bios": [{"key": "100", "version": "100", "description": "old"}],
            "firmware": [{"key": "PD|1.0", "version": "1.0"}],
            "intel_me": [{"key": "ME|1.0", "version": "1.0"}],
            "cpu_qvl": [{"cpu": "CPU A", "bios_version": "all"}, {"cpu": "CPU B"}],
        }
        new = {
            "bios": [
                {"key": "100", "version": "100", "description": "new"},
                {"key": "200", "version": "200"},
            ],
            "firmware": [
                {"key": "PD|1.0", "version": "1.0"},
                {"key": "PD|2.0", "version": "2.0"},
            ],
            "intel_me": [{"key": "ME|2.0", "version": "2.0"}],
            "cpu_qvl": [{"cpu": "CPU A", "bios_version": "100"}],
        }
        changes = compare(old, new)
        self.assertEqual(changes["bios"]["added"][0]["version"], "200")
        self.assertEqual(changes["bios"]["changed"][0]["after"]["description"], "new")
        self.assertEqual(changes["cpu_qvl"]["removed"][0]["cpu"], "CPU B")
        self.assertEqual(changes["firmware"]["added"][0]["version"], "2.0")
        self.assertEqual(changes["intel_me"]["added"][0]["version"], "2.0")
        self.assertTrue(has_changes(changes))

    def test_summary_contains_items(self):
        empty = {"added": [], "removed": [], "changed": []}
        snapshot = {
            "products": {
                "1": {
                    "product": {
                        "name": "Board",
                        "socket": "AM5",
                        "url": "https://example.com",
                    }
                }
            }
        }
        changes = {"1": {
            "bios": {
                "added": [{
                    "key": "1002",
                    "version": "1002",
                    "title": "",
                    "release_date": "2026/07/15",
                    "beta": True,
                    "description": "Memory update",
                    "download_path": "/bios.zip",
                }],
                "removed": [],
                "changed": [],
            },
            "firmware": {
                "added": [{
                    "key": "PD Firmware|1.29",
                    "version": "1.29",
                    "title": "PD Firmware",
                    "release_date": "2026/07/15",
                    "beta": False,
                    "description": "PD update",
                    "download_path": "/pd.zip",
                }],
                "removed": [],
                "changed": [],
            },
            "intel_me": {
                "added": [{
                    "key": "MEUpdateTool|19.0",
                    "version": "19.0",
                    "title": "MEUpdateTool",
                    "release_date": "2026/07/15",
                    "beta": False,
                    "description": "ME update",
                    "download_path": "/me.zip",
                }],
                "removed": [],
                "changed": [],
            },
            "cpu_qvl": empty,
        }}
        summary = render_summary(snapshot, changes)
        self.assertIn("1002", summary)
        self.assertIn("Memory update", summary)
        self.assertIn("https://dlcdnets.asus.com/bios.zip", summary)
        self.assertIn("[PD Firmware / 韌體]", summary)
        self.assertIn("PD Firmware 1.29", summary)
        self.assertIn("[Intel ME]", summary)
        self.assertIn("MEUpdateTool 19.0", summary)

    def test_asus_release_group_mapping(self):
        self.assertEqual(release_group_key("BIOS"), "bios")
        self.assertEqual(release_group_key("韌體"), "firmware")
        self.assertEqual(release_group_key("Intel ME"), "intel_me")
        self.assertIsNone(release_group_key("Unknown"))

    def test_v1_state_migration(self):
        config = {
            "products": [{
                "socket": "AM5",
                "product_url": "https://rog.asus.com/tw/motherboards/rog-strix/board/",
            }]
        }
        old = {
            "schema_version": 1,
            "product": {
                "name": "Board",
                "url": "https://rog.asus.com/tw/motherboards/rog-strix/board/",
                "m1_id": 123,
            },
            "bios": [{"version": "1002"}],
            "cpu_qvl": [],
        }
        migrated = normalize_old_state(old, config)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["products"]["123"]["product"]["socket"], "AM5")
        self.assertEqual(migrated["products"]["123"]["bios"][0]["key"], "1002")

    @patch("monitor.send_email")
    @patch("monitor.SmtpConfig.from_env")
    def test_email_mode_does_not_query_asus(self, from_env, send_email):
        from_env.return_value = SmtpConfig(
            host="smtp.example.com",
            port=587,
            username="sender@example.com",
            password="secret",
            sender="sender@example.com",
            recipient="recipient@example.com",
            starttls=True,
        )
        with patch("monitor.make_snapshot") as make_snapshot, patch(
            "sys.argv", ["monitor.py", "--test-email"]
        ):
            self.assertEqual(monitor.main(), 0)
        send_email.assert_called_once()
        make_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
