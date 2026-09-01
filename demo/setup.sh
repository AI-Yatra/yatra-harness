#!/bin/sh
# Build a throwaway git repository from demo/tictactoe, with a bare remote.
#
# Seed mode (--seed demo/tictactoe) needs none of this: the harness builds its
# own git history in the run workspace. This exists for the delivery demo,
# which needs a real repository with a real `origin` for the branch to land
# on -- and a local bare repo is the honest way to rehearse that without
# opening a pull request on somebody's account.
#
#   eval "$(demo/setup.sh)"      # sets DEMO_REPO and DEMO_ORIGIN
#
# Shell assignments go to stdout and progress to stderr, so this can be
# eval'd. Every run makes a fresh directory under TMPDIR; nothing is reused,
# and nothing is cleaned up for you.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_dir="$here/tictactoe"
base=$(mktemp -d "${TMPDIR:-/tmp}/yatra-demo-XXXXXX")
origin="$base/origin.git"
repo="$base/tictactoe"

mkdir -p "$repo"
# The tests are copied like everything else, and the task protects them. A
# demo where the agent could quietly edit the specification to make it pass
# would not be demonstrating anything worth watching.
tar -cf - -C "$source_dir" --exclude __pycache__ --exclude '*.pyc' . | tar -xf - -C "$repo"

cd "$repo"
git init -q -b main
git add -A
git -c user.name="Demo" -c user.email="demo@local.invalid" \
    commit -q -m "tictactoe: a game with a flaw and a gap"

# The bare remote is cloned from the working copy rather than populated the
# other way round, so this script never needs write access to anything.
git clone -q --bare "$repo" "$origin"
git remote add origin "$origin"
git fetch -q origin

echo "demo repository: $repo" >&2
echo "its origin:      $origin" >&2
echo "DEMO_REPO=$repo; DEMO_ORIGIN=$origin; export DEMO_REPO DEMO_ORIGIN"
