#!/bin/sh
set -eu
umask 077
config=/etc/orbit-proxy/Caddyfile
caddy=/usr/local/bin/caddy
credentials=/etc/quant-desk-web.password
host=myok888.top
stage=$(mktemp -d /etc/orbit-proxy/.quant.XXXXXXXX)
restore=0
cleanup() {
    if [ "$restore" = 1 ]; then
        install -m 0644 "$stage/original" "$config"
        systemctl restart orbit-proxy
    fi
    rm -rf -- "$stage"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
systemctl is-active --quiet quant-desk
systemctl is-active --quiet orbit-proxy

if [ ! -s "$credentials" ]; then
    /usr/bin/python3.11 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$stage/password"
    install -m 0600 "$stage/password" "$credentials"
fi
hash=$("$caddy" hash-password < "$credentials")
test -n "$hash"
cat > "$stage/route" <<'CADDY'
 redir /quant /quant/ 308
 handle_path /quant/* {
  route {
   basic_auth {
    quant QUANT_PASSWORD_HASH
   }
   @foreign_write {
    method POST
    not header Origin https://myok888.top
   }
   respond @foreign_write 403
   header Cache-Control "no-store"
   reverse_proxy 127.0.0.1:8765 {
    header_up Host 127.0.0.1:8765
    header_up Origin http://127.0.0.1:8765
    header_up -Authorization
    header_up -Cookie
   }
  }
 }
CADDY
cp "$config" "$stage/original"
/usr/bin/python3.11 - "$stage" "$hash" <<'PY'
import re
import sys
from pathlib import Path

stage = Path(sys.argv[1])
original = (stage / 'original').read_text()
if re.search(r'^\s*(?:handle_path|redir)\s+/quant(?:/|\s)', original, re.M):
    sys.exit('已有 /quant 路由，请先核对现有配置。')
site = re.compile(r'^(?:https://)?myok888\.top\s*\{[^\S\n]*$', re.M)
route = (stage / 'route').read_text().replace('QUANT_PASSWORD_HASH', sys.argv[2])
candidate, count = site.subn(lambda match: match[0] + '\n' + route.rstrip('\n'), original)
if count != 1:
    sys.exit('未找到唯一的 myok888.top 配置块。')
(stage / 'candidate').write_text(candidate)
PY

if ! "$caddy" validate --config "$stage/candidate" --adapter caddyfile > "$stage/validation" 2>&1; then
    if ! grep -q 'unrecognized directive: basic_auth' "$stage/validation"; then
        cat "$stage/validation" >&2
        exit 1
    fi
    sed 's/   basic_auth {/   basicauth {/' "$stage/candidate" > "$stage/compatible"
    mv "$stage/compatible" "$stage/candidate"
    "$caddy" validate --config "$stage/candidate" --adapter caddyfile
fi

restore=1
install -m 0644 "$stage/candidate" "$config"
systemctl restart orbit-proxy
url="https://$host/quant/strategy"
for attempt in 1 2 3 4 5; do
    if [ "$(curl -sS --max-time 3 --resolve "$host:443:127.0.0.1" -o /dev/null -w '%{http_code}' "$url")" = 401 ] &&
       printf 'user = "quant:%s"\n' "$(cat "$credentials")" |
           curl -K - -fsS --max-time 3 --resolve "$host:443:127.0.0.1" "$url" > "$stage/status.json" &&
       /usr/bin/python3.11 -c 'import json,sys; assert isinstance(json.load(sys.stdin)["running"], bool)' < "$stage/status.json"; then
        restore=0
        printf 'Quant Desk 入口已验证： https://%s/quant/\n' "$host"
        exit 0
    fi
    sleep 1
done
echo 'Quant Desk HTTPS 入口未通过验证，恢复原代理配置。' >&2
exit 1
