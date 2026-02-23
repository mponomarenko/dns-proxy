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
from typing import Dict, Iterable, List, Union

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

    def update_hosts(self, dns_map: Dict[str, Union[str, List[str]]]) -> None:
        hosts_list = []
        for host, ip_or_ips in dns_map.items():
            if isinstance(ip_or_ips, list):
                for ip in ip_or_ips:
                    hosts_list.append(f"{ip} {host}")
            else:
                hosts_list.append(f"{ip_or_ips} {host}")
        payload = {"config": {"dns": {"hosts": sorted(hosts_list)}}}
        if self.debug:
            print(
                f"[DEBUG] Updating Pi-hole with {len(hosts_list)} host entries",
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


def load_overrides(file_path: str) -> Dict[str, str]:
    """Load DNS overrides from hosts-format file: 'IP hostname [alias...]'"""
    if not file_path or not os.path.exists(file_path):
        return {}
    result = {}
    with open(file_path, "r") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                ip = parts[0]
                for hostname in parts[1:]:
                    result[hostname] = ip
    return result


def apply_overrides(
    dns_map: Dict[str, str],
    overrides: Dict[str, str],
    debug: bool = False,
) -> Dict[str, str]:
    """Apply static overrides, taking precedence over Avahi-discovered hosts."""
    updated = dict(dns_map)
    for host, ip in overrides.items():
        existing_ip = updated.get(host)
        if existing_ip is None:
            print(f"[INFO] Override: {host} -> {ip}")
        elif existing_ip != ip:
            print(f"[INFO] Override: {host} {existing_ip} -> {ip}")
        else:
            _debug_log(debug, f"Override unchanged for {host}; remains {ip}")
            continue
        updated[host] = ip
    return updated


def sync_iteration(
    pihole_client: "PiHoleClient",
    avahi_client: "AvahiClient",
    domain_suffix: str,
    keep_local: bool,
    overrides: Dict[str, str] = None,
    debug: bool = False,
) -> Dict[str, str]:
    dns_map = pihole_client.fetch_hosts()

    records = avahi_client.discover_hosts(domain_suffix, keep_local=keep_local)
    avahi_debug = {record.fqdn: list(record.candidates) for record in records}
    _debug_log(debug, f"Avahi hosts discovered: {avahi_debug}")

    updated = apply_avahi_records(dns_map, records, overrides=overrides, debug=debug)

    # Apply overrides (for hosts not discovered via Avahi)
    if overrides:
        updated = apply_overrides(updated, overrides, debug=debug)

    _debug_log(
        debug,
        f"Updating Pi-hole with {len(updated)} hosts: {sorted(updated.items())}",
    )

    pihole_client.update_hosts(updated)
    return updated


def apply_avahi_records(
    dns_map: Dict[str, str],
    records: Iterable[HostRecord],
    overrides: Dict[str, str] = None,
    debug: bool = False,
) -> Dict[str, str]:
    updated = dict(dns_map)
    overrides = overrides or {}
    for record in records:
        host = record.fqdn
        preferred_ip = record.preferred_ip
        if not preferred_ip:
            print(
                f"[WARN] No preferred IP resolved for {record.base_name}.local; skipping",
                file=sys.stderr,
            )
            continue
        existing_ip = updated.get(host)
        has_override = host in overrides
        # Always log Avahi discoveries (useful for finding hosts after OS reinstall)
        if existing_ip is None or existing_ip != preferred_ip:
            suffix = " (override active)" if has_override else ""
            print(f"[INFO] Avahi: {host} -> {preferred_ip}{suffix}")
        # Skip Pi-hole update if host has static override
        if has_override:
            continue
        if existing_ip == preferred_ip:
            _debug_log(debug, f"No change for {host}; remains {existing_ip}")
            continue
        updated[host] = preferred_ip

    # Create subdomain variants: .any (all IPs), .v4 (IPv4 only), .v6 (IPv6 only)
    seen_bases = set()
    for record in records:
        if record.base_name in seen_bases:
            continue
        seen_bases.add(record.base_name)
        if not record.all_ips:
            continue

        # Split IPs by address family
        v4_ips = [ip for ip in record.all_ips if ":" not in ip]
        v6_ips = [ip for ip in record.all_ips if ":" in ip]

        # Build subdomain hostnames: base_name.{any,v4,v6}.domain
        parts = record.fqdn.rsplit(".", 1)
        if len(parts) == 2:
            base, domain = parts
        else:
            base, domain = record.fqdn, ""

        variants = [
            ("any", list(record.all_ips)),
            ("v4", v4_ips),
            ("v6", v6_ips),
        ]

        for variant, ips in variants:
            if not ips:
                continue
            variant_host = f"{base}.{variant}.{domain}" if domain else f"{base}.{variant}"
            if variant_host in overrides:
                continue
            existing = updated.get(variant_host)
            if existing != ips:
                _debug_log(debug, f"{variant}: {variant_host} -> {ips}")
                updated[variant_host] = ips if len(ips) > 1 else ips[0]

    return updated


def parse_targets(api_env: str, token_env: str) -> List[tuple]:
    """Parse comma-separated Pi-hole API URLs and tokens into target pairs."""
    apis = [url.strip() for url in api_env.split(",") if url.strip()]
    tokens = [tok.strip() for tok in token_env.split(",") if tok.strip()]

    if len(apis) == 1 and len(tokens) > 1:
        # Single API with multiple tokens - replicate API for each token
        apis = apis * len(tokens)
    elif len(tokens) == 1 and len(apis) > 1:
        # Multiple APIs with single token - replicate token for each API
        tokens = tokens * len(apis)
    elif len(apis) != len(tokens):
        raise RuntimeError(
            f"Mismatch: {len(apis)} PIHOLE_API URLs but {len(tokens)} PIHOLE_TOKEN values"
        )

    return list(zip(apis, tokens))


def main() -> None:
    pihole_api = os.getenv("PIHOLE_API", "http://10.0.0.2/api")
    pihole_token = os.getenv("PIHOLE_TOKEN")
    domain_suffix = os.getenv("DOMAIN_SUFFIX", "home")
    debug_enabled = os.getenv("DEBUG", "0") == "1"
    keep_local = os.getenv("KEEP_LOCAL", "0") == "1"
    overrides_file = os.getenv("DNS_OVERRIDES_FILE", "/config/overrides")

    if not pihole_token:
        print("[ERROR] Missing API token (PIHOLE_TOKEN)", file=sys.stderr)
        sys.exit(1)

    _debug_log(debug_enabled, "Debug logging enabled")

    targets = parse_targets(pihole_api, pihole_token)
    if len(targets) > 1:
        print(f"[INFO] Configured {len(targets)} Pi-hole targets (fan-out mode)")

    overrides = load_overrides(overrides_file)
    if overrides:
        print(f"[INFO] Loaded {len(overrides)} DNS overrides: {list(overrides.keys())}")

    avahi_client = AvahiClient(debug=debug_enabled)
    pihole_clients: List[PiHoleClient] = []
    errors = []

    multi = len(targets) > 1
    target_name = lambda i: f"Pi-hole #{i}" if multi else "Pi-hole"

    try:
        # Connect to all Pi-hole targets
        for i, (api_url, token) in enumerate(targets, 1):
            try:
                client = PiHoleClient(api_url, token, debug=debug_enabled)
                pihole_clients.append(client)
                _debug_log(debug_enabled, f"{target_name(i)}: connected to {api_url}")
            except RuntimeError as exc:
                errors.append(f"{target_name(i)} ({api_url}): {exc}")

        if not pihole_clients:
            print("[ERROR] Failed to connect to any Pi-hole targets:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)

        if errors:
            print(f"[WARN] Failed to connect to {len(errors)} target(s):", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)

        # Sync to all connected targets
        for i, client in enumerate(pihole_clients, 1):
            try:
                sync_iteration(
                    client,
                    avahi_client,
                    domain_suffix,
                    keep_local=keep_local,
                    overrides=overrides,
                    debug=debug_enabled,
                )
                _debug_log(debug_enabled, f"{target_name(i)}: sync complete")
            except RuntimeError as exc:
                print(f"[ERROR] {target_name(i)}: {exc}", file=sys.stderr)

    finally:
        for client in pihole_clients:
            client.close()

    print(f"[INFO] Sync complete ({len(pihole_clients)} target(s)).")


if __name__ == "__main__":
    main()
