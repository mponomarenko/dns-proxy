# TODO

Gitea tracking: these items are `mikep/dns-proxy#1`–`#3`, in source order,
including the first resilience item filed during the audit. Full details live
in Gitea.

- Make multi-target Pi-hole sync resilient when one target is down. Current behavior can crash the process if a replica such as `10.0.0.3` refuses connections, including during client cleanup. Expected behavior: log the failed target, continue syncing healthy targets, and keep the daemon alive for the next interval.
- Document/deployment guardrail: run dns-proxy with a restart policy such as `unless-stopped`, not `no`, so a transient Pi-hole or network failure does not leave the service dead.
- Fix Avahi discovery for hosts that only appear as `IPv6` browse rows inside the container. On `master`, the host Avahi sees `dev.local -> 10.0.19.182`, but inside the dns-proxy container `dev SSH` appears only as an IPv6 service (`2601:...`). The current parser filters primary candidates to IPv4 browse rows, so it drops `dev` and leaves stale Pi-hole records.
- [x] #4 Bound Avahi browse/resolve subprocesses so hangs cannot bypass the one-hour outage deadline.
- [ ] #5 Discover mDNS once per cycle and fan out one frozen snapshot to every reachable Pi-hole.
- [ ] #6 Detect partial mDNS views and reject unsafe address replacements while preserving existing records.
- [ ] #7 Read back and verify Pi-hole updates before counting a target as successful.
- [ ] #8 Track and report per-target Pi-hole health while retaining the “at least one works” availability rule.
- [ ] #9 Use monotonic, restart-visible outage timing so restarts cannot hide a continuous outage.
- [ ] #10 Add a functional heartbeat/success Docker health check for hung or degraded proxies.
- [ ] #11 Compare canonical mDNS fingerprints across proxies; safely address disagreement, stale ownership, and IPv6-only discovery.
