# TODO

- Make multi-target Pi-hole sync resilient when one target is down. Current behavior can crash the process if a replica such as `10.0.0.3` refuses connections, including during client cleanup. Expected behavior: log the failed target, continue syncing healthy targets, and keep the daemon alive for the next interval.
- Document/deployment guardrail: run dns-proxy with a restart policy such as `unless-stopped`, not `no`, so a transient Pi-hole or network failure does not leave the service dead.
- Fix Avahi discovery for hosts that only appear as `IPv6` browse rows inside the container. On `master`, the host Avahi sees `dev.local -> 10.0.19.182`, but inside the dns-proxy container `dev SSH` appears only as an IPv6 service (`2601:...`). The current parser filters primary candidates to IPv4 browse rows, so it drops `dev` and leaves stale Pi-hole records.
