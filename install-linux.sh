#!/bin/sh
set -eu
stage=$(cd -- "${1:-.}" && pwd)
app_dir=/opt/quant-desk
unit=/etc/systemd/system/quant-desk.service
files="app.py strategy_signals.py backtest_replay.py index.html backtest.html index.css backtest.css index.js backtest.js"

if [ -e "$app_dir" ] || [ -L "$app_dir" ] || [ -e "$unit" ] || [ -L "$unit" ] ||
   systemctl cat quant-desk.service >/dev/null 2>&1; then
    echo '服务器已有量化程序目录或服务，请先核对现有部署。' >&2
    exit 1
fi

/usr/bin/python3.11 -c "from http.server import ThreadingHTTPServer"
/usr/bin/python3.11 -m py_compile "$stage/app.py" "$stage/strategy_signals.py" "$stage/backtest_replay.py"
mkdir -m 0755 "$app_dir"
committed=0
rollback() {
    if [ "$committed" = 0 ]; then
        systemctl disable --now quant-desk.service >/dev/null 2>&1 || true
        rm -f -- "$unit"
        for file in $files; do rm -f -- "$app_dir/$file"; done
        rmdir -- "$app_dir" 2>/dev/null || true
        systemctl daemon-reload || true
    fi
}
trap rollback EXIT
for file in $files; do install -m 0644 "$stage/$file" "$app_dir/"; done

cat > "$stage/quant-desk.service" <<'UNIT'
[Unit]
Description=Quant Desk Binance Demo
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
DynamicUser=yes
StateDirectory=quant-desk
WorkingDirectory=/opt/quant-desk
Environment=HOME=/var/lib/quant-desk
Environment=BROWSER=/bin/false
EnvironmentFile=-/etc/quant-desk.env
ExecStart=/usr/bin/python3.11 -u /opt/quant-desk/app.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
install -m 0644 "$stage/quant-desk.service" "$unit"
systemctl daemon-reload
systemctl enable --now quant-desk.service

attempt=0
while [ "$attempt" -lt 10 ]; do
    if systemctl is-active --quiet quant-desk.service &&
       curl -fsS --max-time 2 http://127.0.0.1:8765/strategy > "$stage/status.json" &&
       /usr/bin/python3.11 -c 'import json, sys; assert "running" in json.load(sys.stdin)' < "$stage/status.json"; then
        committed=1
        cat "$stage/status.json"
        printf '\n量化后台已启动。\n'
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

journalctl -u quant-desk.service -n 15 --no-pager >&2
echo '量化后台未通过启动检查，正在撤销本次安装。' >&2
exit 1
