import unittest

from monitor import compare, has_changes, product_path, render_summary


class MonitorTests(unittest.TestCase):
    def test_product_path_removes_support_suffix(self):
        url = "https://rog.asus.com/tw/motherboards/rog-strix/model/helpdesk_bios/"
        self.assertEqual(product_path(url), "tw/motherboards/rog-strix/model/")

    def test_detects_add_remove_and_change(self):
        old = {
            "bios": [{"version": "100", "description": "old"}],
            "cpu_qvl": [{"cpu": "CPU A", "bios_version": "all"}, {"cpu": "CPU B"}],
        }
        new = {
            "bios": [{"version": "100", "description": "new"}, {"version": "200"}],
            "cpu_qvl": [{"cpu": "CPU A", "bios_version": "100"}],
        }
        changes = compare(old, new)
        self.assertEqual(changes["bios"]["added"][0]["version"], "200")
        self.assertEqual(changes["bios"]["changed"][0]["after"]["description"], "new")
        self.assertEqual(changes["cpu_qvl"]["removed"][0]["cpu"], "CPU B")
        self.assertTrue(has_changes(changes))

    def test_summary_contains_items(self):
        snapshot = {"product": {"name": "Board", "url": "https://example.com"}}
        changes = {
            "bios": {"added": [{"version": "1002", "release_date": "2026/07/15", "beta": True}], "removed": [], "changed": []},
            "cpu_qvl": {"added": [], "removed": [], "changed": []},
        }
        self.assertIn("1002", render_summary(snapshot, changes))


if __name__ == "__main__":
    unittest.main()
