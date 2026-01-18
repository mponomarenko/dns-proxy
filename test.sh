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

sudo docker buildx build "$@" --load -t mikesplay/dns-proxy:test .

# daemon not running mode
# sudo docker run --rm --env-file .env mikesplay/dns-proxy:test

# pass thru mode
sudo docker run --rm \
    --network=host \
    -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket \
    --env-file .env \
    mikesplay/dns-proxy:test
