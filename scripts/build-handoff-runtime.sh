#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! -d $1 ]]; then
    printf 'usage: %s OUTPUT_DIRECTORY\n' "$0" >&2
    exit 2
fi
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    printf 'The Quarry handoff runtime must be built on Linux x86_64.\n' >&2
    exit 1
fi

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
output=$(cd -- "$1" && pwd -P)
uv_version=0.13.9
python_version=3.13.18
node_version=v24.22.3
markdownlint_version=0.24.2
bundle=quarry-linux-amd64-v1.tar.gz

require_tool() {
    command -v "$1" || { printf 'Required tool not found: %s\n' "$1" >&2; exit 1; }
}
tar_tool=$(require_tool tar)
if [[ $("$tar_tool" --version) != *'GNU tar'* ]]; then
    printf 'GNU tar is required.\n' >&2
    exit 1
fi
uv_tool=$(require_tool uv)
node_tool=$(require_tool node)
curl_tool=$(require_tool curl)
node_tool=$(env -i PATH=/usr/bin:/bin "$node_tool" -p 'process.execPath')
node_home=$(dirname -- "$(dirname -- "$node_tool")")
if [[ $("$uv_tool" --version) != "uv $uv_version "* || $("$node_tool" --version) != "$node_version" ]]; then
    printf 'Use uv %s and the upstream Node %s distribution.\n' "$uv_version" "$node_version" >&2
    exit 1
fi
if [[ ! -s $node_home/LICENSE || ! -f $node_home/lib/node_modules/npm/bin/npx-cli.js ]]; then
    printf 'Use a Node distribution with its license, npm and npx.\n' >&2
    exit 1
fi
python_tool=$(
    "$uv_tool" --no-config python find --managed-python --no-project --system "$python_version"
) || {
    printf 'Install the managed interpreter first: uv python install %s\n' "$python_version" >&2
    exit 1
}
python_home=$(env -i "$python_tool" -I -c 'import sys; print(sys.base_prefix)')

work=$(mktemp -d "$output/.quarry-runtime.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
runtime=$work/runtime
mkdir -p "$runtime/bin" "$runtime/node/bin" "$runtime/node/lib/node_modules" \
    "$runtime/cache/uv" "$runtime/cache/npm" "$runtime/licenses" "$work/home" "$work/tmp"
install -m 0755 "$uv_tool" "$runtime/bin/uv"
install -m 0755 "$node_tool" "$runtime/node/bin/node"
cp -R -P "$node_home/lib/node_modules/npm" "$runtime/node/lib/node_modules/npm"
ln -s ../lib/node_modules/npm/bin/npm-cli.js "$runtime/node/bin/npm"
ln -s ../lib/node_modules/npm/bin/npx-cli.js "$runtime/node/bin/npx"
cp -R -P "$python_home" "$runtime/python"
python_tool=$runtime/python/bin/python3.13
install -m 0644 "$node_home/LICENSE" "$runtime/licenses/node-LICENSE"
for notice in LICENSE-APACHE LICENSE-MIT; do
    "$curl_tool" --proto '=https' --proto-redir '=https' --fail --silent --show-error \
        --location --connect-timeout 15 --max-time 180 --output "$runtime/licenses/uv-$notice" \
        "https://raw.githubusercontent.com/astral-sh/uv/$uv_version/$notice"
done

isolated=(env -i "HOME=$work/home" "TMPDIR=$work/tmp"
    "PATH=$runtime/bin:$runtime/node/bin:/usr/bin:/bin" LC_ALL=C TZ=UTC PYTHONDONTWRITEBYTECODE=1)
uv_env=("${isolated[@]}" "UV_PYTHON=$python_tool" UV_PYTHON_DOWNLOADS=never
    "UV_CACHE_DIR=$runtime/cache/uv" "UV_PROJECT_ENVIRONMENT=$work/venv" UV_LINK_MODE=copy)
"${uv_env[@]}" uv --directory "$repo" sync --locked --extra dev --no-install-project >&2
# Seed the build backend separately: uv.lock does not resolve PEP 518 build requirements.
"${uv_env[@]}" uv --no-config pip install --only-binary :all: --python "$work/venv/bin/python" \
    "uv-build==$uv_version" >&2

# npx resolves the global binary before looking at the registry. Keep both
# the pinned installation and its complete npm cache in the verified runtime.
"${isolated[@]}" "npm_config_cache=$runtime/cache/npm" npm_config_ignore_scripts=true \
    npm_config_audit=false npm_config_fund=false npm_config_update_notifier=false \
    npm install --global --prefix "$runtime/node" "markdownlint-cli2@$markdownlint_version" >&2

# The locked Pyright wheel bundles its JS; verify the wrapper can run it with the pinned Node, without npm downloads.
pyright_version=$("${isolated[@]}" "$work/venv/bin/python" -c 'import importlib.metadata; print(importlib.metadata.version("pyright"))')
pyright_output=$("${isolated[@]}" "npm_config_cache=$runtime/cache/npm" npm_config_offline=true \
    "PYRIGHT_PYTHON_FORCE_VERSION=$pyright_version" PYRIGHT_PYTHON_IGNORE_WARNINGS=true \
    "$work/venv/bin/pyright" --version)
if [[ $pyright_output != "pyright $pyright_version" ]]; then
    printf 'Expected bundled pyright %s, got: %s\n' "$pyright_version" "$pyright_output" >&2
    exit 1
fi
printf 'Offline checker verified: %s\n' "$pyright_output" >&2
rm -rf -- "$runtime/cache/npm/_logs"
rm -f -- "$runtime/cache/npm/_update-notifier-last-checked"
{
    "${isolated[@]}" uv --version
    "${isolated[@]}" "$python_tool" --version
    "${isolated[@]}" node --version
    "${isolated[@]}" npm --version
    printf 'markdownlint-cli2 %s\n' "$markdownlint_version"
} > "$runtime/versions.txt"

# Package-manager cache contents can change between builds despite normalized
# archive headers. Pin the digest of the published asset, not a local rebuild.
LC_ALL=C "$tar_tool" --sort=name --format=gnu --mtime=@0 --numeric-owner \
    --owner=0 --group=0 --hard-dereference -C "$runtime" -cf - bin cache licenses node python versions.txt \
    | gzip -n > "$work/runtime.tar.gz"
sha=$(
    "$python_tool" -I -c 'import hashlib, sys; f = open(sys.argv[1], "rb"); print(hashlib.file_digest(f, "sha256").hexdigest())' \
        "$work/runtime.tar.gz"
)
archive=$output/$sha.tar.gz
mv -- "$work/runtime.tar.gz" "$archive"
printf 'Bundle %s is an Ethos display name. To install the digest-named archive:\n' "$bundle" >&2
printf "  install -Dm644 -- %q \"\$HOME/.punt-labs/ethos/runtimes/%s.tar.gz\"\n" "$archive" "$sha" >&2
printf '%s\n' "$archive"
