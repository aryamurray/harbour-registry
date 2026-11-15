#!/usr/bin/env bash
# protobuf through the registry: ONE consumer project with TWO programs, because `protobuf` and
# `protobuf-lite` must never be linked into the same executable (the full library contains the lite
# sources), while tools/test.py's default runner insists on exactly one `exe` target per consumer.
# Both programs build in the same project, so the dependency is built once and `[test] objects` still
# counts one `deps/protobuf-<version>` directory.
#
# Contract (see tools/test.py): cwd = $REGISTRY_TEST_WORK, $HARBOUR is the binary, HOME is an isolated
# home whose registry list is the checkout under test. Exit 0 and print `OK ...` last.
set -euo pipefail

work="$REGISTRY_TEST_WORK"
fail() { echo "== FAILED: $*" >&2; exit 1; }

cp -R "$REGISTRY_TEST_DIR/consumer" "$work/consumer"
cd "$work/consumer"

"$HARBOUR" build --strict-sources
# Nothing changed, so nothing may recompile (a fingerprint that is always dirty hides real staleness).
if "$HARBOUR" build --strict-sources 2>&1 | grep -q "Compiling"; then
  fail "the second build recompiled something although nothing changed"
fi

run() {
  local exe
  exe="$(find .harbour/target \( -path "*/bin/$1" -o -path "*/bin/$1.exe" \) -print -quit)"
  [ -n "$exe" ] && [ -x "$exe" ] || fail "the executable \`$1\` was not built"
  "$exe"
}

run pb-lite
run pb-full
