#!/bin/sh
set -eu
umask 077
app=/opt/quant-desk
repo=https://github.com/Gh811/quant-desk.git
raw=https://raw.githubusercontent.com/Gh811/quant-desk
status_url=http://127.0.0.1:8765/strategy
export GIT_TERMINAL_PROMPT=0

exec 9>/run/lock/quant-desk-update.lock
flock -n 9 || exit 0
stage=$(mktemp -d "$app/.update.XXXXXXXX")
restore=0
cleanup() {
    if [ "$restore" = 1 ]; then
        systemctl stop quant-desk || true
        install -m 0644 "$stage/old-app.py" "$app/app.py"
        install -m 0644 "$stage/old-index.html" "$app/index.html"
        systemctl start quant-desk
        echo '已恢复上一个版本。'
    fi
    rm -rf -- "$stage"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if command -v git >/dev/null 2>&1; then
    remote=$(git ls-remote --exit-code "$repo" refs/heads/main | cut -f1)
else
    curl -fsS --connect-timeout 10 --max-time 25 \
        https://api.github.com/repos/Gh811/quant-desk/commits/main > "$stage/head.json"
    remote=$(/usr/bin/python3.11 -c 'import json,sys; print(json.load(sys.stdin)["sha"])' < "$stage/head.json")
fi
printf '%s\n' "$remote" | grep -Eq '^[0-9a-f]{40}$'
if [ -f "$app/.deployed-commit" ] && [ "$(cat "$app/.deployed-commit")" = "$remote" ]; then
    exit 0
fi

if command -v git >/dev/null 2>&1; then
    git clone --quiet --depth 1 --single-branch --branch main "$repo" "$stage/repo"
    revision=$(git -C "$stage/repo" rev-parse HEAD)
else
    revision=$remote
    mkdir "$stage/repo"
    for file in app.py index.html; do
        curl -fsS --connect-timeout 10 --max-time 25 "$raw/$revision/$file" -o "$stage/repo/$file"
    done
fi
for file in app.py index.html; do
    test -f "$stage/repo/$file" && test ! -L "$stage/repo/$file"
done
/usr/bin/python3.11 -m py_compile "$stage/repo/app.py"

if ! cmp -s "$app/app.py" "$stage/repo/app.py" || ! cmp -s "$app/index.html" "$stage/repo/index.html"; then
    curl -fsS --max-time 5 "$status_url" > "$stage/status.json"
    if ! /usr/bin/python3.11 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(not (s["running"] is False and not s.get("draining") and not s.get("switch_blocked")))' < "$stage/status.json"; then
        echo '策略正在运行或处于切换状态，延后部署。'
        exit 0
    fi

    cp "$app/app.py" "$stage/old-app.py"
    cp "$app/index.html" "$stage/old-index.html"
    restore=1
    systemctl stop quant-desk
    install -m 0644 "$stage/repo/app.py" "$stage/repo/index.html" "$app/"
    systemctl start quant-desk
    healthy=0
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        if systemctl is-active --quiet quant-desk && curl -fsS --max-time 2 "$status_url" > "$stage/status.json" &&
           /usr/bin/python3.11 -c 'import json,sys; assert isinstance(json.load(sys.stdin)["running"], bool)' < "$stage/status.json"; then
            healthy=1
            break
        fi
        sleep 1
    done
    test "$healthy" = 1
    restore=0
fi
printf '%s\n' "$revision" > "$stage/revision"
mv -f "$stage/revision" "$app/.deployed-commit"
printf '已同步 GitHub main：%s\n' "$revision"
