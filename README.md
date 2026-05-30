<!--
Copyright 2025 Mike Ponomarenko

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# DNS Proxy

[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)](https://hub.docker.com/r/mikesplay/dns-proxy)
[![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg?style=for-the-badge)](LICENSE)

> A Docker container that seamlessly converts mDNS `.local` records into regular DNS records served by Pi-hole

**Author:** Mike Ponomarenko

## Why?

I have way too many devices and I do not want to let Pi-Hole manage my DNS manually.
This allows me to have human readable "host.home" or "host.local" records that I can actually use from within my other containers - namely Prometheus.

This container will continuously monitor mDNS records and use Pi-hole API to update them.

It can convert foo.local into foo.home, or leave as foo.local if you are feeling adventurous.

## Features

- **Dockerized Solution** - Easy deployment and management
- **mDNS Discovery** - Automatically discovers `.local` devices on your network
- **Pi-hole Integration** - Syncs discovered devices to your Pi-hole DNS server
- **Multiple Pi-hole Targets** - Fan-out updates to multiple Pi-hole instances (master/slave setups)
- **IPv6 Support** - Discovers both IPv4 and IPv6 addresses with `.v4`, `.v6`, and `.any` subdomain variants
- **Lightweight** - Minimal resource footprint
- **Real-time Sync** - Continuously monitors and updates DNS records

## Quick Start

Before you begin - make sure to create .env file, most importantly:
```bash
PIHOLE_API=http://10.0.0.2/api
PIHOLE_TOKEN=yadayadayada
```

### Multiple Pi-hole Instances

For master/slave or redundant Pi-hole setups, you can sync to multiple instances:

```bash
# Multiple targets with different tokens
PIHOLE_API=http://10.0.0.2/api,http://10.0.0.3/api
PIHOLE_TOKEN=token_for_pihole1,token_for_pihole2

# Multiple targets with same token (e.g., replicas)
PIHOLE_API=http://10.0.0.2/api,http://10.0.0.3/api
PIHOLE_TOKEN=shared_token
```

### Using Docker Hub

```bash
sudo docker run  \
    --restart=unless-stopped \
    --network=host \
    -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket \
    --env-file .env \
    mikesplay/dns-proxy:test
```

Use `unless-stopped` (not the default `no`) so a transient Pi-hole or network failure does not leave the service dead.

### Building from Source

```bash
# Build and test locally
./test.sh

# Build for current platform
./build.sh

# Build for multiple platforms (ARM64 + x64)
./build-all.sh
```

## Project Structure

```text
dns-proxy/
├── dockerfile          # Container configuration
├── avahi.py           # Avahi mDNS wrapper
├── sync.py            # Pi-hole synchronization logic
├── run.sh             # Application entry point
├── test.sh            # Testing script
├── build.sh           # Build script (current platform)
├── build-all.sh       # Multi-platform build script
└── tests/             # Test suite
    └── test_sync.py
```

## How It Works

The application consists of three main components:

1. **Avahi Wrapper** (`avahi.py`)
   - Packages and manages the Avahi daemon
   - Discovers mDNS services on the local network

2. **Synchronizer** (`sync.py`)
   - Extracts IP addresses from discovered services
   - Pushes DNS records to Pi-hole

3. **Entry Point** (`run.sh`)
   - Orchestrates all components
   - Manages the application lifecycle

## Testing

Run the test suite to ensure everything is working correctly:

```bash
./test.sh
```

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
