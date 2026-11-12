#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
orbit_record="$HOME/Desktop/orbit-market-lab/.local-data/deploy"
orbit_host=root@47.237.182.131
ssh_options=(
    -o BatchMode=yes -o ConnectTimeout=10 -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$orbit_record/known_hosts"
    -i "$orbit_record/ecs-singapore-20261015"
)

remote_stage=$(ssh "${ssh_options[@]}" "$orbit_host" \
    'test -d /opt/orbit/current && umask 077 && mktemp -d /tmp/quant-desk.XXXXXXXX')
if [[ ! "$remote_stage" =~ ^/tmp/quant-desk\.[A-Za-z0-9]+$ ]]; then
    echo '无法确认 Orbit 服务器的上传目录。' >&2
    exit 1
fi

cleanup() {
    ssh "${ssh_options[@]}" "$orbit_host" "rm -rf -- '$remote_stage'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

scp "${ssh_options[@]}" "$source_dir/app.py" "$source_dir/index.html" "$source_dir/install-linux.sh" "$orbit_host:$remote_stage/"

ssh "${ssh_options[@]}" "$orbit_host" "sh '$remote_stage/install-linux.sh' '$remote_stage'"
