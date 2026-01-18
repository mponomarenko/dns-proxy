#!/usr/bin/bash

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

# Multi-platform build for both ARM64 and x64 architectures
# Requires docker buildx with multi-platform support
# Uses QEMU v8.1.5 to avoid segfaults in ARM64 emulation (https://github.com/tonistiigi/binfmt/issues/245)

# Set up QEMU with older version to avoid segmentation faults
docker run --privileged --rm tonistiigi/binfmt:qemu-v8.1.5 --install all

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t mikesplay/dns-proxy:latest \
  -t mikesplay/dns-proxy:arm64 \
  -t mikesplay/dns-proxy:amd64 \
  --push .