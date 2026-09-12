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

import hashlib
import json
import os
import socket
import struct
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Union
from urllib.parse import urlsplit

import requests

from avahi import AvahiClient, HostRecord


def _debug_log(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[DEBUG] {message}", file=sys.stderr)


class PiHoleClient:
    # (connect, read) timeout for every Pi-hole API call. Without this, a
    # stalled/unresponsive Pi-hole leaves `requests` blocking forever, which
    # hangs the sync subprocess mid-cycle with no exception and no log output
    # -- the run.sh loop never reaches its sleep/next-iteration, so the
    # container stays "Up" while the sync loop is silently dead forever.
    REQUEST_TIMEOUT = (5, 30)

    def __init__(self, api_url: str, token: str, debug: bool = False):
        self.api_url = api_url
        self.dns_server = urlsplit(api_url).hostname
        if not self.dns_server:
            raise RuntimeError(f"Pi-hole API URL has no hostname: {api_url}")
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
        try:
            auth_resp = self.session.post(
                f"{self.api_url}/auth",
                json={"password": clean_token},
                timeout=self.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Pi-hole connection failed: {exc}") from exc
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

    def fetch_hosts(self) -> Dict[str, Union[str, List[str]]]:
        try:
            resp = self.session.get(
                f"{self.api_url}/config/dns%2Fhosts",
                headers=self.headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Pi-hole fetch failed: {exc}") from exc
        cfg = resp.json()
        dns_map: Dict[str, Union[str, List[str]]] = {}
        for entry in cfg.get("config", {}).get("dns", {}).get("hosts", []):
            parts = entry.split()
            if len(parts) >= 2:
                ip, host = parts[0], parts[1]
                existing = dns_map.get(host)
                if existing is None:
                    dns_map[host] = ip
                elif isinstance(existing, list):
                    if ip not in existing:
                        existing.append(ip)
                elif existing != ip:
                    dns_map[host] = [existing, ip]
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
        try:
            set_resp = self.session.patch(
                f"{self.api_url}/config/dns%2Fhosts",
                headers=self.headers,
                json=payload,
                timeout=self.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Pi-hole update failed: {exc}") from exc
        if not set_resp.ok:
            raise RuntimeError(
                f"Failed to update Pi-hole config: {set_resp.status_code} {set_resp.text}"
            )

    def verify_hosts(self, dns_map: Dict[str, Union[str, List[str]]]) -> None:
        failures = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            checks = {
                executor.submit(resolve_dns_a, self.dns_server, hostname): (
                    hostname,
                    {ip} if isinstance(ip, str) else set(ip),
                )
                for hostname, ip in dns_map.items()
            }
            for future in as_completed(checks):
                hostname, expected = checks[future]
                try:
                    observed = future.result()
                except (OSError, IndexError, struct.error) as exc:
                    failures.append(f"{hostname}: DNS query failed: {exc}")
                    continue
                if not observed.intersection(expected):
                    failures.append(
                        f"{hostname}: expected one of {sorted(expected)}, "
                        f"resolved {sorted(observed)}"
                    )
        if failures:
            preview = "; ".join(failures[:10])
            remainder = len(failures) - min(len(failures), 10)
            if remainder:
                preview += f"; and {remainder} more"
            raise RuntimeError(
                f"Pi-hole DNS self-check failed for {len(failures)} name(s): {preview}"
            )
        print(f"[INFO] Pi-hole DNS self-check passed for {len(dns_map)} name(s)")

    def close(self) -> None:
        try:
            del_resp = self.session.delete(
                f"{self.api_url}/auth",
                headers=self.headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            if not del_resp.ok:
                print(
                    f"[WARN] Pi-hole logout failed: {del_resp.status_code} {del_resp.text.strip()}",
                    file=sys.stderr,
                )
        except (requests.RequestException, OSError) as exc:
            print(f"[WARN] Pi-hole logout request failed: {exc}", file=sys.stderr)
        finally:
            self.session.close()


def load_overrides(file_path: str, avahi_client: Optional["AvahiClient"] = None) -> Dict[str, Union[str, List[str]]]:
    """Load DNS overrides from hosts-format file: 'IP-or-.local hostname [alias...]'

    The first field may be a static IP or a .local hostname to resolve via Avahi.
    """
    if not file_path:
        return {}
    try:
        f_obj = open(file_path, "r")
    except FileNotFoundError:
        return {}
    result = {}
    with f_obj:
        for line in f_obj:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                ip_or_host = parts[0]
                if ip_or_host.endswith(".local"):
                    if avahi_client is None:
                        print(f"[WARN] Override: cannot resolve {ip_or_host} without avahi client; skipping", file=sys.stderr)
                        continue
                    ip = avahi_client.resolve_hostname(ip_or_host)
                    if not ip:
                        print(f"[WARN] Override: could not resolve {ip_or_host}; skipping", file=sys.stderr)
                        continue
                else:
                    ip = ip_or_host
                for hostname in parts[1:]:
                    existing = result.get(hostname)
                    if existing is None:
                        result[hostname] = ip
                    elif isinstance(existing, list):
                        if ip not in existing:
                            existing.append(ip)
                    elif existing != ip:
                        result[hostname] = [existing, ip]
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
    overrides: Dict[str, Union[str, List[str]]] = None,
    debug: bool = False,
    min_mdns_hosts: int = 1,
    records: Optional[List[HostRecord]] = None,
) -> Dict[str, Union[str, List[str]]]:
    dns_map = pihole_client.fetch_hosts()

    if records is None:
        records = avahi_client.discover_hosts(domain_suffix, keep_local=keep_local)
        validate_mdns_view(records, min_mdns_hosts)
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
    pihole_client.verify_hosts(updated)
    return updated


def encode_dns_name(hostname: str) -> bytes:
    labels = hostname.rstrip(".").split(".")
    return b"".join(bytes([len(label)]) + label.encode("idna") for label in labels) + b"\0"


def skip_dns_name(packet: bytes, offset: int) -> int:
    while True:
        length = packet[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += length + 1


def resolve_dns_a(server: str, hostname: str, timeout: float = 2.0) -> set:
    transaction_id = int.from_bytes(os.urandom(2), "big")
    header = struct.pack("!HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0)
    packet = header + encode_dns_name(hostname) + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, 53))
        response, _ = sock.recvfrom(4096)

    if len(response) < 12:
        raise OSError("short DNS response")
    response_id, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", response[:12])
    if response_id != transaction_id:
        raise OSError("DNS transaction ID mismatch")
    if flags & 0x000F:
        raise OSError(f"DNS response error code {flags & 0x000F}")
    offset = 12
    for _ in range(questions):
        offset = skip_dns_name(response, offset) + 4
    addresses = set()
    for _ in range(answers):
        offset = skip_dns_name(response, offset)
        record_type, record_class, _, data_length = struct.unpack(
            "!HHIH", response[offset : offset + 10]
        )
        offset += 10
        data = response[offset : offset + data_length]
        offset += data_length
        if record_type == 1 and record_class == 1 and data_length == 4:
            addresses.add(socket.inet_ntoa(data))
    return addresses


def verify_mdns_records(avahi_client: AvahiClient, records: List[HostRecord]) -> None:
    failures = []
    unique_records = {}
    for record in records:
        unique_records.setdefault(record.base_name, record)
    for base_name, record in sorted(unique_records.items()):
        observed = avahi_client.resolve_hostname(f"{base_name}.local")
        if not observed or observed not in record.candidates:
            failures.append(
                f"{base_name}.local: expected one of {list(record.candidates)}, "
                f"resolved {observed or '<none>'}"
            )
    if failures:
        preview = "; ".join(failures[:10])
        remainder = len(failures) - min(len(failures), 10)
        if remainder:
            preview += f"; and {remainder} more"
        raise RuntimeError(
            f"mDNS self-check failed for {len(failures)} name(s): {preview}"
        )
    print(f"[INFO] mDNS self-check passed for {len(unique_records)} name(s)")


def _log_mdns_view(records: List[HostRecord]) -> None:
    discovered_hosts = {record.base_name for record in records}
    view = "\n".join(
        f"{record.fqdn}|{record.preferred_ip}|{','.join(sorted(set(record.candidates)))}|"
        f"{','.join(sorted(set(record.all_ips)))}"
        for record in sorted(records, key=lambda item: item.fqdn)
    )
    fingerprint = hashlib.sha256(view.encode("utf-8")).hexdigest()[:12]
    print(
        f"[INFO] mDNS view: hosts={len(discovered_hosts)} records={len(records)} "
        f"fingerprint={fingerprint}"
    )


def validate_mdns_view(
    records: List[HostRecord],
    min_mdns_hosts: int,
    baseline_hosts: int = 0,
    baseline_ratio: float = 0.7,
) -> int:
    _log_mdns_view(records)
    discovered_hosts = {record.base_name for record in records}
    if min_mdns_hosts and len(discovered_hosts) < min_mdns_hosts:
        raise RuntimeError(
            f"mDNS view below safety threshold: discovered {len(discovered_hosts)} "
            f"host(s), expected at least {min_mdns_hosts}"
        )
    required_hosts = max(
        min_mdns_hosts,
        int(baseline_hosts * baseline_ratio + 0.9999),
    )
    if baseline_hosts and len(discovered_hosts) < required_hosts:
        raise RuntimeError(
            f"mDNS view below recent baseline: discovered {len(discovered_hosts)} "
            f"host(s), baseline is {baseline_hosts} and minimum ratio is "
            f"{baseline_ratio:.0%}"
        )
    return len(discovered_hosts)


def load_mdns_baseline(path: str) -> int:
    if not path:
        return 0
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        return max(0, int(state.get("host_count", 0)))
    except (OSError, ValueError, TypeError):
        return 0


def save_mdns_baseline(path: str, host_count: int) -> None:
    if not path:
        return
    temporary_path = f"{path}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump({"host_count": host_count}, handle)
        os.replace(temporary_path, path)
    except OSError as exc:
        print(f"[WARN] Could not save mDNS baseline: {exc}", file=sys.stderr)
        try:
            os.unlink(temporary_path)
        except OSError:
            pass


def apply_avahi_records(
    dns_map: Dict[str, Union[str, List[str]]],
    records: Iterable[HostRecord],
    overrides: Dict[str, Union[str, List[str]]] = None,
    debug: bool = False,
) -> Dict[str, Union[str, List[str]]]:
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
        if existing_ip is not None:
            previous_ips = existing_ip if isinstance(existing_ip, list) else [existing_ip]
            if preferred_ip not in previous_ips:
                print(
                    f"[WARN] Preserving existing address(es) for {host}: "
                    f"{previous_ips}; adding observed {preferred_ip}"
                )
                updated[host] = [*previous_ips, preferred_ip]
        else:
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


def main() -> bool:
    pihole_api = os.getenv("PIHOLE_API", "http://10.0.0.2/api")
    pihole_token = os.getenv("PIHOLE_TOKEN")
    domain_suffix = os.getenv("DOMAIN_SUFFIX", "home")
    debug_enabled = os.getenv("DEBUG", "0") == "1"
    keep_local = os.getenv("KEEP_LOCAL", "0") == "1"
    overrides_file = os.getenv("DNS_OVERRIDES_FILE", "/config/overrides")
    static_hosts_env = os.getenv("DNS_STATIC_HOSTS", "")
    min_mdns_hosts = int(os.getenv("MIN_MDNS_HOSTS", "1"))
    mdns_baseline_file = os.getenv("MDNS_BASELINE_FILE", "/config/mdns-baseline.json")
    mdns_baseline_ratio = float(os.getenv("MDNS_BASELINE_RATIO", "0.7"))
    baseline_hosts = load_mdns_baseline(mdns_baseline_file)

    if not pihole_token:
        print("[ERROR] Missing API token (PIHOLE_TOKEN)", file=sys.stderr)
        sys.exit(1)

    _debug_log(debug_enabled, "Debug logging enabled")

    targets = parse_targets(pihole_api, pihole_token)
    if len(targets) > 1:
        print(f"[INFO] Configured {len(targets)} Pi-hole targets (fan-out mode)")

    avahi_client = AvahiClient(debug=debug_enabled)

    overrides = load_overrides(overrides_file, avahi_client=avahi_client)

    # Merge DNS_STATIC_HOSTS env var: comma-separated "IP=hostname" pairs
    # e.g. DNS_STATIC_HOSTS=10.0.0.50=compute.home,10.0.0.51=other.home
    if static_hosts_env:
        for entry in static_hosts_env.split(","):
            entry = entry.strip()
            if "=" not in entry:
                print(f"[WARN] DNS_STATIC_HOSTS: skipping invalid entry '{entry}' (expected IP=hostname)", file=sys.stderr)
                continue
            ip, hostname = entry.split("=", 1)
            overrides[hostname.strip()] = ip.strip()

    if overrides:
        print(f"[INFO] Loaded {len(overrides)} DNS overrides: {list(overrides.keys())}")
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
            print(
                "[WARN] No Pi-hole targets available; retrying next interval.",
                file=sys.stderr,
            )
            return False

        if errors:
            print(f"[WARN] Failed to connect to {len(errors)} target(s):", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)

        try:
            records = avahi_client.discover_hosts(domain_suffix, keep_local=keep_local)
            discovered_host_count = validate_mdns_view(
                records,
                min_mdns_hosts,
                baseline_hosts=baseline_hosts,
                baseline_ratio=mdns_baseline_ratio,
            )
            verify_mdns_records(avahi_client, records)
        except RuntimeError as exc:
            print(f"[ERROR] mDNS discovery failed: {exc}", file=sys.stderr)
            return False

        # Sync the same validated mDNS snapshot to all connected targets. A
        # connected target whose sync fails
        # does not count as a successful cycle; the caller uses this result to
        # enforce a bounded outage budget instead of hiding a permanent outage.
        successful_syncs = 0
        for i, client in enumerate(pihole_clients, 1):
            try:
                sync_iteration(
                    client,
                    avahi_client,
                    domain_suffix,
                    keep_local=keep_local,
                    overrides=overrides,
                    debug=debug_enabled,
                    min_mdns_hosts=min_mdns_hosts,
                    records=records,
                )
                _debug_log(debug_enabled, f"{target_name(i)}: sync complete")
                successful_syncs += 1
            except RuntimeError as exc:
                print(f"[ERROR] {target_name(i)}: {exc}", file=sys.stderr)

    finally:
        for client in pihole_clients:
            client.close()

    if successful_syncs:
        save_mdns_baseline(mdns_baseline_file, discovered_host_count)
        print(f"[INFO] Sync complete ({successful_syncs}/{len(pihole_clients)} target(s)).")
        return True
    print("[WARN] No Pi-hole target completed a sync; retrying next interval.", file=sys.stderr)
    return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
