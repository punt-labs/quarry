#!/usr/bin/env bash
# Rebuild the chat database and sync it to Fly.io.
#
# The chat database is a curated subset of the punt-labs workspace:
# READMEs, DESIGN.md, CHANGELOGs, prfaq PDFs, research files, blog posts,
# and the public website's content collections, data files, and key pages.
# It serves the punt-labs.com chat widget via quarry.fly.dev.
#
# Usage: ./scripts/sync-chat-db.sh
#
# Prerequisites:
#   - quarry CLI installed
#   - fly CLI authenticated (fly auth login)
#   - python3 available (for JSON→markdown conversion)
#   - Workspace at ~/Coding/punt-labs/
#   - For full coverage: cd public-website && npx astro build (renders HTML pages)

set -euo pipefail
shopt -s nullglob

WORKSPACE="${QUARRY_WORKSPACE:-$HOME/Coding/punt-labs}"
APP="quarry"
TARBALL="/tmp/chat-lancedb.tar.gz"
REMOTE_PATH="/data/default"
SCRATCH="${WORKSPACE}/.tmp"

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
echo "--- Reading list ---"
for f in public-website/src/content/reading/*.md; do
  quarry ingest "$f" --collection public-website --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
echo "--- Press releases ---"
for f in public-website/src/content/press/*.md; do
  quarry ingest "$f" --collection public-website --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
echo "--- Demos ---"
for f in public-website/src/content/demos/*.md; do
  quarry ingest "$f" --collection public-website --overwrite 2>&1 | grep "^Done:" || true
done

echo ""
echo "--- Research files ---"
# Use python3 to handle filenames with spaces safely.
# Covers both top-level research/ and per-project */research/ directories.
python3 -c "
import subprocess, pathlib
for pattern in ('research/{ext}', '*/research/{ext}'):
    for ext in ('*.md', '*.pdf', '*.docx'):
        for f in sorted(pathlib.Path('.').glob(pattern.format(ext=ext))):
            proj = f.parts[0] if len(f.parts) > 2 else 'workspace'
            r = subprocess.run(
                ['quarry', 'ingest', str(f), '--collection', proj, '--overwrite'],
                capture_output=True, text=True,
            )
            for line in r.stdout.splitlines():
                if line.startswith('Done:'):
                    print(line)
                    break
"

echo ""
echo "--- Project data (JSON→markdown) ---"
mkdir -p "$SCRATCH"
SCRATCH_DIR="$SCRATCH" python3 -c "
import json, pathlib, os
out = pathlib.Path(os.environ['SCRATCH_DIR'])
data = json.loads(pathlib.Path('public-website/src/data/projects.json').read_text())
lines = ['# Punt Labs Projects\n']
for p in data:
    lines.append(f\"## {p['name']}\n\")
    lines.append(f\"**Tagline:** {p['tagline']}\n\")
    lines.append(f\"{p['description']}\n\")
    if p.get('features'):
        lines.append('**Features:**\n')
        for feat in p['features']:
            lines.append(f'- {feat}')
        lines.append('')
    lines.append(f\"**Category:** {p['category']} | **Stage:** {p.get('stage','—')} | **Version:** {p.get('version','—')}\")
    if p.get('installCommand'):
        lines.append(f\"**Install:** \`{p['installCommand']}\`\")
    lines.append('')
(out / 'projects-catalog.md').write_text('\n'.join(lines))
"
quarry ingest "$SCRATCH/projects-catalog.md" --collection public-website --overwrite 2>&1 | grep "^Done:" || true

echo ""
echo "--- Technology radar (JSON→markdown) ---"
SCRATCH_DIR="$SCRATCH" python3 -c "
import json, pathlib, os
out = pathlib.Path(os.environ['SCRATCH_DIR'])
data = json.loads(pathlib.Path('public-website/src/data/radar.json').read_text())
quadrant_names = {q['id']: q['name'] for q in data.get('quadrants', [])}
ring_names = {r['id']: r['name'] for r in data.get('rings', [])}
lines = ['# Punt Labs Technology Radar\n']
for entry in data.get('entries', []):
    label = entry.get('label', '?')
    quadrant = quadrant_names.get(entry.get('quadrant', ''), '?')
    ring = ring_names.get(entry.get('ring', ''), '?')
    desc = entry.get('description', '')
    lines.append(f'## {label}')
    lines.append(f'**Quadrant:** {quadrant} | **Ring:** {ring}')
    if desc:
        lines.append(desc)
    lines.append('')
(out / 'technology-radar.md').write_text('\n'.join(lines))
"
quarry ingest "$SCRATCH/technology-radar.md" --collection public-website --overwrite 2>&1 | grep "^Done:" || true

echo ""
echo "--- Website pages (rendered HTML) ---"
DIST="public-website/dist/client"
if [ -d "$DIST" ]; then
  for page in about grounding building-blocks building-blocks/integration applications radar demos; do
    html="$DIST/$page/index.html"
    if [ -f "$html" ]; then
      quarry ingest "$html" --collection public-website --overwrite 2>&1 | grep "^Done:" || true
    fi
  done
  # Homepage
  if [ -f "$DIST/index.html" ]; then
    quarry ingest "$DIST/index.html" --collection public-website --overwrite 2>&1 | grep "^Done:" || true
  fi
else
  echo "WARNING: $DIST not found — skipping rendered pages."
  echo "Run 'cd public-website && npx astro build' first for full coverage."
fi

echo ""
quarry status

echo ""
echo "=== Creating tarball ==="
# COPYFILE_DISABLE suppresses macOS ._* resource fork files.
# --no-xattrs prevents PAX xattr headers (com.apple.provenance) that cause
# GNU tar on Linux to exit with error.
xattr -cr "$HOME/.quarry/data/chat/lancedb" 2>/dev/null || true
COPYFILE_DISABLE=1 tar czf "$TARBALL" --no-xattrs -C "$HOME/.quarry/data/chat" lancedb
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
# fly ssh -C doesn't invoke a shell, so use sh -c for chained commands
fly ssh console -a "$APP" -C "sh -c 'rm -rf ${REMOTE_PATH}/lancedb && tar xzf /data/chat-lancedb.tar.gz -C ${REMOTE_PATH}/ && rm /data/chat-lancedb.tar.gz'"

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
rm -f "$SCRATCH/projects-catalog.md" "$SCRATCH/technology-radar.md"
