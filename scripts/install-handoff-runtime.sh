#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    echo 'No Quarry handoff runtime is published for this platform (requires Linux x86_64).' >&2
    exit 1
fi

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
sidecar=$repo/.punt-labs/ethos/checks/snapshots/quarry-default.yaml
pin=$(python3 -I - "$sidecar" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as profile:
        entry = json.load(profile)["checks"]["full-gate"]["runtime"]["platforms"]["linux-amd64"]
    url, sha = entry.get("url"), entry["sha256"]
except (AttributeError, OSError, KeyError, TypeError, ValueError):
    sys.exit("Invalid Quarry handoff runtime profile.")
expected = (r"https://github\.com/punt-labs/quarry/releases/download/"
            r"handoff-runtime-v[1-9][0-9]*/quarry-handoff-linux-amd64\.tar\.gz")
if url is not None and (not isinstance(url, str) or not re.fullmatch(expected, url)):
    sys.exit("Invalid Quarry handoff runtime release URL.")
if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
    sys.exit("Invalid Quarry handoff runtime SHA-256.")
print(sha, url or "")
PY
)
read -r sha url <<< "$pin"

root=${HOME:?}/.punt-labs
store=$root/ethos/runtimes
archive=$store/$sha.tar.gz
umask 077
for dir in "$root" "$root/ethos" "$store"; do
    if [[ -L $dir ]]; then
        printf 'Refusing symlink runtime directory: %s\n' "$dir" >&2
        exit 1
    fi
    mkdir -p -- "$dir"
done
if [[ -L $archive || ( -e $archive && ! -f $archive ) ]]; then
    printf 'Refusing non-regular runtime archive: %s\n' "$archive" >&2
    exit 1
fi

verify() {
    local actual
    actual=$(sha256sum < "$1") && [[ ${actual%% *} == "$sha" ]]
}
if [[ -f $archive ]] && verify "$archive"; then
    printf '%s\n' "$archive"
    exit 0
fi
if [[ -z $url ]]; then
    echo 'No published Quarry handoff runtime URL is pinned in this checkout.' >&2
    exit 1
fi

download=$(mktemp "$store/.quarry-runtime.XXXXXX")
trap 'rm -f -- "$download"' EXIT
curl --disable --proto '=https' --proto-redir '=https' --fail --silent --show-error \
    --connect-timeout 15 --retry 2 --retry-max-time 120 --max-time 300 \
    --max-filesize 1073741824 --location --output "$download" -- "$url"
if ! verify "$download"; then
    printf 'Quarry handoff runtime checksum mismatch; expected %s.\n' "$sha" >&2
    exit 1
fi
mv -fT -- "$download" "$archive"
printf '%s\n' "$archive"
