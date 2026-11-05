#!/bin/sh
# Smoke test: the Changes piece on its own, in a clean environment, against the stand-in gh. Read, list, a page, the feed.
#
#   sh test/smoke.sh
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo '[{"slug":"app","name":"The App","links":{"repos":["acme/app"]}}]' > "$tmp/cards.json"
run() { env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$tmp/home" LOOM_PIECE_HOME="$tmp/piece" \
  CHANGES_GH="$here/test/fake-gh" FAKE_GH_LOG="$tmp/gh.log" FAKE_GH_FIXTURE="$here/test/fixtures/github-list.json" \
  CHANGES_PROJECTS="cat $tmp/cards.json" CHANGES_TASKS="false" CHANGES_APPROVALS="false" \
  CHANGES_AGENTS='{"ada":{"logins":["ada-bot"]}}' "$here/bin/changes" "$@"; }
fail() { echo "smoke: FAIL: $*" >&2; exit 1; }

run --help | grep -q "loom-changes sync" || fail "--help"
run sync --once | grep -q "read 1 repos" || fail "sync"
run list | grep -q "github:acme/app#1 " || fail "list"
echo '{"path":""}' | run ui page --json | grep -q '"title": "Changes"' || fail "index"
echo '{"path":"c/github/acme/app/1"}' | run ui page --json | grep -q '"title": "app#1"' || fail "a change"
run project-feed --json --project app --since 2026-11-01T00:00:00Z --repo github:acme/app | grep -q '"groups"' || fail "feed"
run ui --json | grep -q '"label": "Changes"' || fail "manifest"
[ -f "$tmp/piece/data/snapshot.json" ] || fail "the snapshot isn't under the piece home"
grep -v '"auth", "token"\|"api", "graphql"' "$tmp/gh.log" && fail "gh was asked for something other than a read"
echo "smoke: ok (one repo read, its changes listed and drawn; gh asked for reads only)"
