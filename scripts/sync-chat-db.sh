#!/usr/bin/env bash
# Rebuild the chat database and sync it to Fly.io.
#
# The chat database is a curated subset of the punt-labs workspace:
# READMEs, DESIGN.md, CHANGELOGs, prfaq PDFs, and blog posts.
# It serves the punt-labs.com chat widget via quarry.fly.dev.
#
# Usage: ./scripts/sync-chat-db.sh
#
# Prerequisites:
#   - quarry CLI installed
#   - fly CLI authenticated (fly auth login)
#   - Workspace at ~/Coding/punt-labs/

set -euo pipefail

WORKSPACE="${QUARRY_WORKSPACE:-$HOME/Coding/punt-labs}"
APP="quarry"
TARBALL="/tmp/chat-lancedb.tar.gz"
REMOTE_PATH="/data/default"

echo "=== Rebuilding chat database ==="
quarry use chat

cd "$WORKSPACE"

echo ""
echo "--- READMEs ---"
for f in */README.md; do
  proj=$(dirname "$f")
  quarry ingest "$f" --collection "$proj" --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
echo "--- DESIGN.md ---"
for f in */DESIGN.md; do
  proj=$(dirname "$f")
  quarry ingest "$f" --collection "$proj" --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
echo "--- CHANGELOGs ---"
for f in */CHANGELOG.md; do
  proj=$(dirname "$f")
  quarry ingest "$f" --collection "$proj" --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
echo "--- PR/FAQs ---"
for f in */prfaq.pdf; do
  proj=$(dirname "$f")
  quarry ingest "$f" --collection "$proj" --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
echo "--- Blog posts ---"
for f in public-website/src/content/blog/*.md; do
  quarry ingest "$f" --collection public-website --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
quarry status

echo ""
echo "=== Creating tarball ==="
# COPYFILE_DISABLE suppresses macOS ._* resource fork files that break LanceDB on Linux
COPYFILE_DISABLE=1 tar czf "$TARBALL" -C "$HOME/.quarry/data/chat" lancedb
ls -lh "$TARBALL"

echo ""
echo "=== Uploading to Fly.io ==="
# Wake the machine first (auto-stop may have stopped it)
echo "Waking machine..."
curl -sf https://quarry.fly.dev/health > /dev/null 2>&1 || sleep 5

fly sftp shell -a "$APP" <<EOF
put $TARBALL /data/chat-lancedb.tar.gz
EOF

echo ""
echo "=== Extracting on Fly.io ==="
fly ssh console -a "$APP" -C "rm -rf ${REMOTE_PATH}/lancedb && tar xzf /data/chat-lancedb.tar.gz -C ${REMOTE_PATH}/ && rm /data/chat-lancedb.tar.gz"

echo ""
echo "=== Restarting server ==="
MACHINE_ID=$(fly machines list -a "$APP" --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
fly machine restart "$MACHINE_ID" -a "$APP"

echo ""
echo "=== Waiting for server ==="
sleep 10

echo ""
echo "=== Verifying ==="
API_KEY=$(security find-generic-password -a quarry -s quarry-api-key -w 2>/dev/null || echo "")
if [ -n "$API_KEY" ]; then
  curl -sf "https://quarry.fly.dev/search?q=test&limit=1" \
    -H "Authorization: Bearer $API_KEY" | python3 -m json.tool | head -5
  echo "..."
else
  echo "No API key in keychain — skipping verification"
fi

echo ""
echo "=== Done ==="
rm -f "$TARBALL"
