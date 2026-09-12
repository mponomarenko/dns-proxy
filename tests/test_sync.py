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
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from avahi import HostRecord
import requests

import sync
from avahi import AvahiClient
from sync import (
    PiHoleClient,
    load_overrides,
    parse_targets,
    sync_iteration,
    validate_mdns_view,
    verify_mdns_records,
)


class FakePiHoleClient:
    def __init__(self, initial_hosts):
        self._initial_hosts = dict(initial_hosts)
        self.updated_hosts = None
        self.verified_hosts = None

    def fetch_hosts(self):
        return dict(self._initial_hosts)

    def update_hosts(self, dns_map):
        self.updated_hosts = dict(dns_map)

    def verify_hosts(self, dns_map):
        self.verified_hosts = dict(dns_map)


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

    def resolve_hostname(self, hostname):
        base_name = hostname.removesuffix(".local")
        for record in self.records:
            if record.base_name == base_name:
                return record.preferred_ip
        return ""


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
        self.assertEqual(expected, pihole.verified_hosts)


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

        expected = {"tower.home": ["10.0.115.4", "10.0.115.5"]}
        self.assertEqual(expected, result)
        self.assertEqual(expected, pihole.updated_hosts)
        self.assertEqual(expected, pihole.verified_hosts)

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


class AvahiTimeoutTests(unittest.TestCase):
    def test_browse_timeout_returns_empty_result(self):
        client = AvahiClient(command_timeout=2)
        with mock.patch(
            "avahi.subprocess.check_output",
            side_effect=subprocess.TimeoutExpired("avahi-browse", 2),
        ) as check_output:
            self.assertEqual("", client._run_browse())

        check_output.assert_called_once()
        self.assertEqual(2, check_output.call_args.kwargs["timeout"])

    def test_resolve_timeout_returns_empty_result(self):
        client = AvahiClient(command_timeout=3)
        with mock.patch(
            "avahi.subprocess.check_output",
            side_effect=subprocess.TimeoutExpired("avahi-resolve-host-name", 3),
        ) as check_output:
            self.assertEqual("", client._resolve_ipv4("dev"))

        check_output.assert_called_once()
        self.assertEqual(3, check_output.call_args.kwargs["timeout"])


class SelfCheckTests(unittest.TestCase):
    def test_mdns_self_check_rejects_conflicting_resolution(self):
        avahi = mock.Mock()
        avahi.resolve_hostname.return_value = "10.0.0.99"
        records = [
            HostRecord(
                base_name="router",
                fqdn="router.home",
                preferred_ip="10.0.0.1",
                candidates=("10.0.0.1",),
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "mDNS self-check failed"):
            verify_mdns_records(avahi, records)

    def test_mdns_baseline_rejects_partial_view(self):
        records = [
            HostRecord(
                base_name="router",
                fqdn="router.home",
                preferred_ip="10.0.0.1",
                candidates=("10.0.0.1",),
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "below recent baseline"):
            validate_mdns_view(records, 1, baseline_hosts=10, baseline_ratio=0.7)


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
    def test_dns_self_check_covers_every_name(self):
        client = object.__new__(PiHoleClient)
        client.dns_server = "10.0.0.2"
        expected = {
            "one.home": "10.0.0.1",
            "two.home": ["10.0.0.2", "10.0.0.3"],
            "three.local": "10.0.0.4",
        }

        def resolved(server, hostname):
            value = expected[hostname]
            return {value} if isinstance(value, str) else {value[0]}

        with mock.patch.object(sync, "resolve_dns_a", side_effect=resolved) as resolver:
            client.verify_hosts(expected)

        self.assertEqual(3, resolver.call_count)
        self.assertEqual(
            {"one.home", "two.home", "three.local"},
            {call.args[1] for call in resolver.call_args_list},
        )

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

    def test_close_tolerates_connection_error(self):
        client = object.__new__(PiHoleClient)
        client.api_url = "http://10.0.0.3/api"
        client.sid = "fake-sid"
        client.session = mock.Mock()
        client.session.delete.side_effect = requests.ConnectionError("refused")

        client.close()
        client.session.close.assert_called_once()

    def test_close_tolerates_os_error(self):
        client = object.__new__(PiHoleClient)
        client.api_url = "http://10.0.0.3/api"
        client.sid = "fake-sid"
        client.session = mock.Mock()
        client.session.delete.side_effect = OSError("dbus disconnected")

        client.close()
        client.session.close.assert_called_once()


class MainLoopFailureTests(unittest.TestCase):
    def test_no_targets_returns_for_next_interval_instead_of_exiting(self):
        with mock.patch.dict(
            os.environ,
            {
                "PIHOLE_API": "http://10.0.0.2/api,http://10.0.0.3/api",
                "PIHOLE_TOKEN": "token1,token2",
                "DNS_OVERRIDES_FILE": "",
                "MIN_MDNS_HOSTS": "0",
            },
            clear=False,
        ), mock.patch.object(sync, "AvahiClient"), mock.patch.object(
            sync, "PiHoleClient", side_effect=RuntimeError("target unavailable")
        ), mock.patch.object(sync, "load_overrides", return_value={}):
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = sync.main()

        self.assertFalse(result)
        self.assertIn("Failed to connect to any Pi-hole targets", stderr.getvalue())
        self.assertIn("retrying next interval", stderr.getvalue())

    def test_all_connected_targets_failing_returns_false(self):
        class FakeClient:
            def close(self):
                pass

        with mock.patch.dict(
            os.environ,
            {
                "PIHOLE_API": "http://10.0.0.2/api,http://10.0.0.3/api",
                "PIHOLE_TOKEN": "token1,token2",
                "DNS_OVERRIDES_FILE": "",
                "MIN_MDNS_HOSTS": "0",
            },
            clear=False,
        ), mock.patch.object(sync, "AvahiClient"), mock.patch.object(
            sync, "PiHoleClient", side_effect=[FakeClient(), FakeClient()]
        ), mock.patch.object(
            sync, "sync_iteration", side_effect=RuntimeError("sync failed")
        ), mock.patch.object(sync, "load_overrides", return_value={}):
            result = sync.main()

        self.assertFalse(result)

    def test_one_successful_target_makes_cycle_successful(self):
        class FakeClient:
            def close(self):
                pass

        with mock.patch.dict(
            os.environ,
            {
                "PIHOLE_API": "http://10.0.0.2/api,http://10.0.0.3/api",
                "PIHOLE_TOKEN": "token1,token2",
                "DNS_OVERRIDES_FILE": "",
                "MIN_MDNS_HOSTS": "0",
            },
            clear=False,
        ), mock.patch.object(sync, "AvahiClient"), mock.patch.object(
            sync, "PiHoleClient", side_effect=[FakeClient(), FakeClient()]
        ), mock.patch.object(
            sync,
            "sync_iteration",
            side_effect=[None, RuntimeError("sync failed")],
        ), mock.patch.object(sync, "load_overrides", return_value={}):
            result = sync.main()

        self.assertTrue(result)

    def test_main_discovers_one_snapshot_for_all_targets(self):
        class FakeClient:
            def close(self):
                pass

        records = [
            HostRecord(
                base_name="router",
                fqdn="router.home",
                preferred_ip="10.0.0.1",
                candidates=("10.0.0.1",),
            )
        ]
        with mock.patch.dict(
            os.environ,
            {
                "PIHOLE_API": "http://10.0.0.2/api,http://10.0.0.3/api",
                "PIHOLE_TOKEN": "token1,token2",
                "DNS_OVERRIDES_FILE": "",
            },
            clear=False,
        ), mock.patch.object(sync, "AvahiClient") as avahi_class, mock.patch.object(
            sync, "PiHoleClient", side_effect=[FakeClient(), FakeClient()]
        ), mock.patch.object(
            sync, "sync_iteration", return_value={}
        ) as sync_mock, mock.patch.object(sync, "load_overrides", return_value={}):
            avahi_class.return_value.discover_hosts.return_value = records
            avahi_class.return_value.resolve_hostname.return_value = "10.0.0.1"
            result = sync.main()

        self.assertTrue(result)
        avahi_class.return_value.discover_hosts.assert_called_once_with(
            "home", keep_local=False
        )
        self.assertEqual(2, sync_mock.call_count)
        self.assertIs(sync_mock.call_args_list[0].kwargs["records"], records)
        self.assertIs(sync_mock.call_args_list[1].kwargs["records"], records)

    def test_empty_mdns_view_is_not_a_successful_sync(self):
        pihole = FakePiHoleClient({"old.home": "10.0.0.10"})
        avahi = MockAvahiClient([])

        with self.assertRaises(RuntimeError) as ctx:
            sync_iteration(pihole, avahi, "home", keep_local=False)

        self.assertIn("mDNS view below safety threshold", str(ctx.exception))
        self.assertIsNone(pihole.updated_hosts)

    def test_empty_mdns_view_never_deletes_existing_records(self):
        existing = {"old.home": "10.0.0.10"}
        pihole = FakePiHoleClient(existing)
        avahi = MockAvahiClient([])

        result = sync_iteration(
            pihole,
            avahi,
            "home",
            keep_local=False,
            min_mdns_hosts=0,
        )

        self.assertEqual(existing, result)
        self.assertEqual(existing, pihole.updated_hosts)

    def test_dns_self_check_failure_marks_sync_failed(self):
        pihole = FakePiHoleClient({})
        pihole.verify_hosts = mock.Mock(side_effect=RuntimeError("DNS mismatch"))
        avahi = MockAvahiClient(
            [
                HostRecord(
                    base_name="router",
                    fqdn="router.home",
                    preferred_ip="10.0.0.1",
                    candidates=("10.0.0.1",),
                )
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "DNS mismatch"):
            sync_iteration(pihole, avahi, "home", keep_local=False)

        self.assertIsNotNone(pihole.updated_hosts)


class LoadOverridesTests(unittest.TestCase):
    def _write_overrides(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".overrides", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_repeated_override_hostname_preserves_all_addresses(self):
        path = self._write_overrides(
            "10.0.0.50 home-api.home\n10.0.0.40 home-api.home\n"
        )
        try:
            self.assertEqual(
                {"home-api.home": ["10.0.0.50", "10.0.0.40"]},
                load_overrides(path),
            )
        finally:
            os.unlink(path)

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
