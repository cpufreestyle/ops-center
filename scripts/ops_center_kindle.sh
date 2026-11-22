#!/bin/sh
# 动态获取 Mac 当前局域网 IPv4，并打开运维值班台 Kindle 看板。
ip="$(ipconfig getifaddr en0 2>/dev/null)"
[ -z "$ip" ] && ip="$(ipconfig getifaddr en1 2>/dev/null)"
[ -z "$ip" ] && ip="$(ipconfig getifaddr en2 2>/dev/null)"
[ -z "$ip" ] && ip="$(ipconfig getifaddr bridge0 2>/dev/null)"
[ -z "$ip" ] && ip="127.0.0.1"
url="http://${ip}:8890/kindle"
if [ "$1" = "--print" ]; then
    printf '%s\n' "$url"
    exit 0
fi
exec open "$url"
