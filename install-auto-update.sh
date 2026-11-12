#!/bin/sh
set -eu
stage=$(cd -- "${1:-.}" && pwd)
systemctl is-active --quiet quant-desk

command -v flock >/dev/null
sh -n "$stage/update-linux.sh"
install -m 0755 "$stage/update-linux.sh" /usr/local/sbin/quant-desk-update

cat > /etc/systemd/system/quant-desk-update.service <<'UNIT'
[Unit]
Description=Update Quant Desk from GitHub
Wants=network-online.target
After=network-online.target quant-desk.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/quant-desk-update
TimeoutStartSec=180
UNIT

cat > /etc/systemd/system/quant-desk-update.timer <<'UNIT'
[Unit]
Description=Check Quant Desk updates every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=quant-desk-update.service

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now quant-desk-update.timer
systemctl start quant-desk-update.service
systemctl is-enabled quant-desk-update.timer
systemctl is-active quant-desk-update.timer quant-desk orbit
systemctl list-timers --all quant-desk-update.timer --no-pager
