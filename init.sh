#!/bin/sh
# The standard startup and verification path for this repository.
#
# One command, so a session -- human or agent -- never has to remember or
# rediscover how this project is checked. Every step is fatal: a partially
# verified tree is the state this script exists to prevent.
set -eu

echo "==> ruff"
uv run ruff check harness tests ay.py

echo "==> tests"
uv run python -m unittest discover -s tests

echo "==> doctor"
uv run harness doctor --config configs/teaching.yaml

echo "==> deterministic run"
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml \
  --skill skills/bugfix.yaml

echo "==> evals"
uv run harness eval evals/teaching.yaml

echo "==> ok"
