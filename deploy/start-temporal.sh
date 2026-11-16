#!/bin/bash
set -euo pipefail

# Docker records the container's address in /etc/hosts. Resolve it with shell
# built-ins because the hardened Temporal image does not ship awk.
read -r hostname < /proc/sys/kernel/hostname
while read -r address name _; do
    if [[ "$name" == "$hostname" ]]; then
        export BIND_ON_IP="$address"
        exec /etc/temporal/entrypoint.sh
    fi
done < /etc/hosts

echo 'Temporal could not find its container address in /etc/hosts.' >&2
exit 1
