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

import unittest

from avahi import HostRecord
from sync import sync_iteration


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


if __name__ == "__main__":
    unittest.main()
