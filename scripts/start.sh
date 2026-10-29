#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"
python -m pip install -r requirements.txt
exec python -m uvicorn app:app --host 127.0.0.1 --port "${PORT:-8000}"
