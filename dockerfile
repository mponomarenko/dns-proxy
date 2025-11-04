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

FROM ubuntu:22.04
#FROM debian:bullseye-slim - this fails on qemu libc-bin

RUN apt-get update && \
    apt-get install -y \
        avahi-daemon \
        avahi-utils \
        bash \
        curl \
        dbus \
        jq \
        python3 \
        python3-pip \
        && \
    apt-get clean

RUN pip3 install requests
    
SHELL ["/bin/bash", "-c"]

COPY sync.py /usr/local/bin/sync.py
RUN chmod +x /usr/local/bin/sync.py

COPY avahi.py /usr/local/bin/avahi.py

COPY run.sh /usr/local/bin/run.sh
RUN chmod +x /usr/local/bin/run.sh

ENTRYPOINT ["/usr/local/bin/run.sh"]
