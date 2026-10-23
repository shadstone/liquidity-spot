#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

if [[ ! -x .venv/bin/python ]]; then
  echo "Create .venv and install requirements-lock.txt before starting the preview." >&2
  exit 1
fi

mkdir -p instance
export DATABASE_URL="sqlite:////${project_dir#/}/instance/shakescape-preview.sqlite"
export SHAKESCAPE_BOARD_FEED_URL="${SHAKESCAPE_BOARD_FEED_URL:-http://127.0.0.1:12039/api/v1/shakescape/direct-offers}"
export SHAKESCAPE_BOARD_NETWORK="${SHAKESCAPE_BOARD_NETWORK:-mainnet}"
export SHAKESCAPE_OFFER_URI_TEMPLATE="${SHAKESCAPE_OFFER_URI_TEMPLATE:-}"
export FLASK_APP=run:app
export FLASK_DEBUG=0
export SECRET_KEY="${SECRET_KEY:-local-shakescape-preview-only}"

echo "Liquidity Spot preview: http://127.0.0.1:${PORT:-8000}/orders"
echo "ShakeScape feed: $SHAKESCAPE_BOARD_FEED_URL"
echo "Using an isolated local preview database; production site orders are not loaded."
exec .venv/bin/python -m flask run --host=127.0.0.1 --port="${PORT:-8000}"
