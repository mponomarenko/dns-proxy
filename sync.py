#!/usr/bin/env python3

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
import sys
from typing import Dict, Iterable

import requests

from avahi import AvahiClient, HostRecord


def _debug_log(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[DEBUG] {message}", file=sys.stderr)


class PiHoleClient:
    def __init__(self, api_url: str, token: str, debug: bool = False):
        self.api_url = api_url
        self.session = requests.Session()
        self.session.headers.update(
            {"accept": "application/json", "content-type": "application/json"}
        )
        self.sid = self._authenticate(token)
        self.debug = debug

    def _authenticate(self, token: str) -> str:
        clean_token = token.strip()
        if not clean_token:
            raise RuntimeError("Empty PIHOLE_TOKEN after stripping whitespace")
        auth_resp = self.session.post(f"{self.api_url}/auth", json={"password": clean_token})
        try:
            auth_resp.raise_for_status()
        except requests.HTTPError as exc:
            detail = auth_resp.text.strip()
            msg = detail or auth_resp.reason or "Unauthorized"
            raise RuntimeError(f"Pi-hole authentication failed: {msg}") from exc
        auth_json = auth_resp.json()
        try:
            return auth_json["session"]["sid"]
        except Exception as exc:  # pragma: no cover - defensive guard
            raise RuntimeError(
                f"Failed to obtain session sid from Pi-hole response: {auth_json!r}"
            ) from exc

    @property
    def headers(self) -> Dict[str, str]:
        return {"accept": "application/json", "sid": self.sid}

    def fetch_hosts(self) -> Dict[str, str]:
        resp = self.session.get(f"{self.api_url}/config/dns%2Fhosts", headers=self.headers)
        resp.raise_for_status()
        cfg = resp.json()
        dns_map: Dict[str, str] = {}
        for entry in cfg.get("config", {}).get("dns", {}).get("hosts", []):
            parts = entry.split()
            if len(parts) >= 2:
                ip, host = parts[0], parts[1]
                dns_map[host] = ip
            else:
                print(f"[WARN] Unexpected hosts entry: {entry}", file=sys.stderr)
        if self.debug:
            for host, ip in sorted(dns_map.items()):
                print(f"[DEBUG] Pi-hole host: {host} -> {ip}", file=sys.stderr)
        return dns_map

    def update_hosts(self, dns_map: Dict[str, str]) -> None:
        hosts_list = [f"{ip} {host}" for host, ip in dns_map.items()]
        payload = {"config": {"dns": {"hosts": sorted(hosts_list)}}}
        if self.debug:
            print(
                f"[DEBUG] Updating Pi-hole hosts payload: {sorted(dns_map.items())}",
                file=sys.stderr,
            )
        set_resp = self.session.patch(
            f"{self.api_url}/config/dns%2Fhosts",
            headers=self.headers,
            json=payload,
        )
        if not set_resp.ok:
            raise RuntimeError(
                f"Failed to update Pi-hole config: {set_resp.status_code} {set_resp.text}"
            )

    def close(self) -> None:
        try:
            del_resp = self.session.delete(f"{self.api_url}/auth", headers=self.headers)
            if not del_resp.ok:
                print(
                    f"[WARN] Pi-hole logout failed: {del_resp.status_code} {del_resp.text.strip()}",
                    file=sys.stderr,
                )
        finally:
            self.session.close()


def sync_iteration(
    pihole_client: "PiHoleClient",
    avahi_client: "AvahiClient",
    domain_suffix: str,
    keep_local: bool,
    debug: bool = False,
) -> Dict[str, str]:
    dns_map = pihole_client.fetch_hosts()

    records = avahi_client.discover_hosts(domain_suffix, keep_local=keep_local)
    if debug:
        avahi_debug = {
            record.fqdn: list(record.candidates) for record in records
        }
        _debug_log(debug, f"Avahi hosts discovered: {avahi_debug}")

    updated = apply_avahi_records(dns_map, records, debug=debug)
    _debug_log(
        debug,
        f"Updating Pi-hole with {len(updated)} hosts: {sorted(updated.items())}",
    )

    pihole_client.update_hosts(updated)
    return updated


def apply_avahi_records(
    dns_map: Dict[str, str],
    records: Iterable[HostRecord],
    debug: bool = False,
) -> Dict[str, str]:
    updated = dict(dns_map)
    for record in records:
        host = record.fqdn
        preferred_ip = record.preferred_ip
        existing_ip = updated.get(host)
        if not preferred_ip:
            print(
                f"[WARN] No preferred IP resolved for {record.base_name}.local; skipping",
                file=sys.stderr,
            )
            continue
        if existing_ip is None:
            print(f"[INFO] Discovered: {host} -> {preferred_ip}")
        elif existing_ip != preferred_ip:
            print(
                f"[WARN] Updating information for {host}: Avahi candidates {list(record.candidates)} -> chosen {preferred_ip}",
                file=sys.stderr,
            )
        else:
            _debug_log(debug, f"No change for {host}; remains {existing_ip}")
            continue
        updated[host] = preferred_ip
    return updated


def main() -> None:
    pihole_api = os.getenv("PIHOLE_API", "http://10.0.0.2/api")
    pihole_token = os.getenv("PIHOLE_TOKEN")
    domain_suffix = os.getenv("DOMAIN_SUFFIX", "home")
    debug_enabled = os.getenv("DEBUG", "0") == "1"
    keep_local = os.getenv("KEEP_LOCAL", "0") == "1"

    if not pihole_token:
        print("[ERROR] Missing API token (PIHOLE_TOKEN)", file=sys.stderr)
        sys.exit(1)

    _debug_log(debug_enabled, "Debug logging enabled")

    avahi_client = AvahiClient(debug=debug_enabled)
    pihole_client = None
    try:
        pihole_client = PiHoleClient(pihole_api, pihole_token, debug=debug_enabled)
        sync_iteration(
            pihole_client,
            avahi_client,
            domain_suffix,
            keep_local=keep_local,
            debug=debug_enabled,
        )
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if pihole_client is not None:
            pihole_client.close()

    print("[INFO] Sync complete.")


if __name__ == "__main__":
    main()
