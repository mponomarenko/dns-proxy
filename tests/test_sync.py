# Copyright 2025 Mike Ponomarenko
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import tempfile
import unittest
from unittest import mock

from avahi import HostRecord
import requests

from sync import PiHoleClient, load_overrides, parse_targets, sync_iteration


class FakePiHoleClient:
    def __init__(self, initial_hosts):
        self._initial_hosts = dict(initial_hosts)
        self.updated_hosts = None

    def fetch_hosts(self):
        return dict(self._initial_hosts)

    def update_hosts(self, dns_map):
        self.updated_hosts = dict(dns_map)


class MockAvahiClient:
    def __init__(self, records):
        self.records = list(records)

    def discover_hosts(self, domain_suffix, keep_local=False):
        normalized = []
        for record in self.records:
            primary = f"{record.base_name}.{domain_suffix}" if domain_suffix else record.base_name
            hostnames = [primary]
            if keep_local and primary != f"{record.base_name}.local":
                hostnames.append(f"{record.base_name}.local")
            for fqdn in hostnames:
                normalized.append(
                    HostRecord(
                        base_name=record.base_name,
                        fqdn=fqdn,
                        preferred_ip=record.preferred_ip,
                        candidates=record.candidates,
                    )
                )
        return normalized


class SyncIterationTests(unittest.TestCase):
    def test_clean_start_discovers_all_hosts(self):
        pihole = FakePiHoleClient({})
        avahi = MockAvahiClient(
            [
                HostRecord(
                    base_name="truenas",
                    fqdn="truenas.local",
                    preferred_ip="10.0.0.10",
                    candidates=("10.0.0.10",),
                )
            ]
        )

        result = sync_iteration(pihole, avahi, "home", keep_local=False)

        expected = {"truenas.home": "10.0.0.10"}
        self.assertEqual(expected, result)
        self.assertEqual(expected, pihole.updated_hosts)

    def test_updates_changed_ip(self):
        pihole = FakePiHoleClient({"tower.home": "10.0.115.4"})
        avahi = MockAvahiClient(
            [
                HostRecord(
                    base_name="tower",
                    fqdn="tower.local",
                    preferred_ip="10.0.115.5",
                    candidates=("10.0.115.5", "10.0.115.4"),
                )
            ]
        )

        result = sync_iteration(pihole, avahi, "home", keep_local=False)

        expected = {"tower.home": "10.0.115.5"}
        self.assertEqual(expected, result)
        self.assertEqual(expected, pihole.updated_hosts)

    def test_adds_missing_host_and_retains_existing(self):
        pihole = FakePiHoleClient({"nas.home": "10.0.0.20"})
        avahi = MockAvahiClient(
            [
                HostRecord(
                    base_name="nas",
                    fqdn="nas.local",
                    preferred_ip="10.0.0.20",
                    candidates=("10.0.0.20",),
                ),
                HostRecord(
                    base_name="printer",
                    fqdn="printer.local",
                    preferred_ip="10.0.0.50",
                    candidates=("10.0.0.50",),
                ),
            ]
        )

        result = sync_iteration(pihole, avahi, "home", keep_local=False)

        expected = {
            "nas.home": "10.0.0.20",
            "printer.home": "10.0.0.50",
        }
        self.assertEqual(expected, result)
        self.assertEqual(expected, pihole.updated_hosts)

    def test_keep_local_adds_local_variant(self):
        pihole = FakePiHoleClient({})
        avahi = MockAvahiClient(
            [
                HostRecord(
                    base_name="tower",
                    fqdn="tower.local",
                    preferred_ip="10.0.115.5",
                    candidates=("10.0.115.5",),
                )
            ]
        )

        result = sync_iteration(pihole, avahi, "home", keep_local=True)

        expected = {
            "tower.home": "10.0.115.5",
            "tower.local": "10.0.115.5",
        }
        self.assertEqual(expected, result)
        self.assertEqual(expected, pihole.updated_hosts)


class ParseTargetsTests(unittest.TestCase):
    def test_single_target(self):
        result = parse_targets("http://10.0.0.2/api", "token1")
        self.assertEqual([("http://10.0.0.2/api", "token1")], result)

    def test_multiple_targets_matching_counts(self):
        result = parse_targets(
            "http://10.0.0.2/api,http://10.0.0.3/api",
            "token1,token2"
        )
        expected = [
            ("http://10.0.0.2/api", "token1"),
            ("http://10.0.0.3/api", "token2"),
        ]
        self.assertEqual(expected, result)


class PiHoleClientTests(unittest.TestCase):
    def test_auth_connection_error_is_wrapped(self):
        client = object.__new__(PiHoleClient)
        client.api_url = "http://10.0.0.3/api"
        client.session = mock.Mock()
        client.session.post.side_effect = requests.ConnectionError("no route to host")

        with self.assertRaises(RuntimeError) as ctx:
            PiHoleClient._authenticate(client, "token")

        self.assertIn("Pi-hole connection failed", str(ctx.exception))

    def test_multiple_apis_single_token(self):
        result = parse_targets(
            "http://10.0.0.2/api,http://10.0.0.3/api",
            "shared_token"
        )
        expected = [
            ("http://10.0.0.2/api", "shared_token"),
            ("http://10.0.0.3/api", "shared_token"),
        ]
        self.assertEqual(expected, result)

    def test_single_api_multiple_tokens(self):
        result = parse_targets(
            "http://10.0.0.2/api",
            "token1,token2"
        )
        expected = [
            ("http://10.0.0.2/api", "token1"),
            ("http://10.0.0.2/api", "token2"),
        ]
        self.assertEqual(expected, result)

    def test_mismatched_counts_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            parse_targets(
                "http://a/api,http://b/api,http://c/api",
                "token1,token2"
            )
        self.assertIn("Mismatch", str(ctx.exception))

    def test_strips_whitespace(self):
        result = parse_targets(
            " http://10.0.0.2/api , http://10.0.0.3/api ",
            " token1 , token2 "
        )
        expected = [
            ("http://10.0.0.2/api", "token1"),
            ("http://10.0.0.3/api", "token2"),
        ]
        self.assertEqual(expected, result)


class LoadOverridesTests(unittest.TestCase):
    def _write_overrides(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".overrides", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_static_ip(self):
        path = self._write_overrides("10.0.0.10 rescue.home\n")
        try:
            self.assertEqual({"rescue.home": "10.0.0.10"}, load_overrides(path))
        finally:
            os.unlink(path)

    def test_static_ip_multiple_aliases(self):
        path = self._write_overrides("10.0.0.11 rescue-fast.home rescue-usb.home\n")
        try:
            result = load_overrides(path)
            self.assertEqual({"rescue-fast.home": "10.0.0.11", "rescue-usb.home": "10.0.0.11"}, result)
        finally:
            os.unlink(path)

    def test_local_hostname_resolved(self):
        avahi = mock.Mock()
        avahi.resolve_hostname.return_value = "10.0.200.116"
        path = self._write_overrides("hdhomerun.local hdhomerun.home\n")
        try:
            result = load_overrides(path, avahi_client=avahi)
            self.assertEqual({"hdhomerun.home": "10.0.200.116"}, result)
            avahi.resolve_hostname.assert_called_once_with("hdhomerun.local")
        finally:
            os.unlink(path)

    def test_local_hostname_resolution_failure_skips(self):
        avahi = mock.Mock()
        avahi.resolve_hostname.return_value = ""
        path = self._write_overrides("missing.local missing.home\n")
        try:
            self.assertEqual({}, load_overrides(path, avahi_client=avahi))
        finally:
            os.unlink(path)

    def test_local_hostname_without_avahi_client_skips(self):
        path = self._write_overrides("hdhomerun.local hdhomerun.home\n")
        try:
            self.assertEqual({}, load_overrides(path, avahi_client=None))
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        self.assertEqual({}, load_overrides("/nonexistent/path/overrides"))

    def test_empty_path_returns_empty(self):
        self.assertEqual({}, load_overrides(""))

    def test_comments_and_blank_lines_ignored(self):
        path = self._write_overrides("# a comment\n\n10.0.0.10 rescue.home # inline\n")
        try:
            self.assertEqual({"rescue.home": "10.0.0.10"}, load_overrides(path))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
