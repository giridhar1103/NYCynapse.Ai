#!/bin/bash
# Release main to the live site without cutting off answers in progress.
#   1. tests must pass   2. wait until no answer is running or queued (up to 3 minutes)
#   3. install, restart  4. the deep health check must pass, or the release is reported failed
# usage: deploy/release.sh   (run from the repository, on main)
set -euo pipefail
cd "$(dirname "$0")/.."
HEALTH=http://127.0.0.1:8031/api/health

branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = main ] || { echo "on $branch, not main"; exit 1; }

echo "== tests"
.venv/bin/ruff check src tests
.venv/bin/pytest -q

echo "== waiting for answers in progress to finish"
for i in $(seq 1 90); do
  stats=$(curl -sf "$HEALTH" || echo '{}')
  busy=$(python3 -c "import json,sys; c=json.loads(sys.argv[1]).get('chat',{}); print(c.get('active',0)+c.get('waiting',0))" "$stats")
  [ "$busy" = 0 ] && break
  [ "$i" = 90 ] && echo "still $busy answering after 3 minutes; restarting anyway"
  sleep 2
done

echo "== install and restart"
.venv/bin/pip install -q -e .
systemctl restart nycynapse-web

echo "== health"
for i in $(seq 1 30); do
  if curl -sf "$HEALTH/deep" >/dev/null; then
    echo "live at $(git log --oneline -1)"
    exit 0
  fi
  sleep 2
done
echo "deep health check failing after restart:"
curl -s "$HEALTH/deep" || true
exit 1
