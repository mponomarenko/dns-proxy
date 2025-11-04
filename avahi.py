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

import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Sequence


@dataclass
class HostRecord:
    base_name: str
    fqdn: str
    preferred_ip: str
    candidates: Sequence[str]


class AvahiClient:
    def __init__(
        self,
        browse_cmd=None,
        resolve_cmd=None,
        debug: bool = False,
    ):
        self._browse_cmd = browse_cmd or ["avahi-browse", "-alrpt", "--parsable"]
        self._resolve_cmd = resolve_cmd or ["avahi-resolve-host-name", "-4"]
        self._debug = debug

    def discover_hosts(self, domain_suffix: str, keep_local: bool = False) -> List[HostRecord]:
        raw = self._run_browse()
        candidates = self._collect_candidates(raw)
        if not candidates:
            print("[ERROR] No IPv4 mDNS records discovered via avahi-browse", file=sys.stderr)
        else:
            debug_map = {f"{name}.local": ips for name, ips in candidates.items()}
            self._log_debug(f"Avahi IPv4 candidates: {debug_map}")
        records = []
        for base_name, ips in candidates.items():
            ordered_ips = list(ips)
            preferred = self._resolve_preferred(base_name, ordered_ips)
            fqdn = f"{base_name}.{domain_suffix}" if domain_suffix else base_name
            suffixes = [fqdn]
            if keep_local and fqdn != f"{base_name}.local":
                suffixes.append(f"{base_name}.local")
            if ordered_ips:
                self._log_debug(
                    f"Avahi candidates for {base_name}.local: {ordered_ips}",
                )
            for fqdn_variant in suffixes:
                records.append(
                    HostRecord(
                        base_name=base_name,
                        fqdn=fqdn_variant,
                        preferred_ip=preferred,
                        candidates=tuple(ordered_ips),
                    )
                )
        return records

    def _run_browse(self) -> str:
        try:
            return subprocess.check_output(
                self._browse_cmd,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
            )
        except subprocess.CalledProcessError:
            return ""

    @staticmethod
    def _collect_candidates(raw: str) -> Dict[str, List[str]]:
        seen = set()
        candidates: Dict[str, List[str]] = {}
        for line in raw.splitlines():
            if "IPv4" not in line:
                continue
            cols = line.split(";")
            try:
                raw_host = cols[6].strip()
                ip_field = cols[7].strip()
            except IndexError:
                continue
            if not raw_host or not ip_field:
                continue
            base_name = raw_host[:-6] if raw_host.endswith(".local") else raw_host
            key = (base_name, ip_field)
            if key in seen:
                continue
            seen.add(key)
            host_ips = candidates.setdefault(base_name, [])
            if ip_field not in host_ips:
                host_ips.append(ip_field)
        return candidates

    def _resolve_preferred(self, base_name: str, candidates: List[str]) -> str:
        resolved_ip = self._resolve_ipv4(base_name)
        if resolved_ip:
            if resolved_ip not in candidates:
                candidates.insert(0, resolved_ip)
            else:
                candidates.remove(resolved_ip)
                candidates.insert(0, resolved_ip)
        return candidates[0] if candidates else resolved_ip or ""

    def _resolve_ipv4(self, base_name: str) -> str:
        mdns_name = f"{base_name}.local"
        try:
            result = subprocess.check_output(
                [*self._resolve_cmd, mdns_name],
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
            )
        except subprocess.CalledProcessError:
            self._log_debug(f"Resolution failed for {mdns_name}")
            return ""
        for line in result.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[-1]:
                self._log_debug(f"Resolved {mdns_name} -> {parts[-1]}")
                return parts[-1]
        self._log_debug(f"No IPv4 address found in resolution output for {mdns_name}")
        return ""

    def _log_debug(self, message: str) -> None:
        if self._debug:
            print(f"[DEBUG] {message}", file=sys.stderr)
