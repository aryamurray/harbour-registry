#!/usr/bin/env bash
# CPython through the registry: the deepest generated-source chain in the
# registry. Build the interpreter as a dependency (host tools freeze CPython's
# own startup modules by running CPython), check the 24 frozen headers are
# byte-identical to CPython's own build, run real Python code, then turn the
# `zlib` feature on and check it changes the generated built-in module table
# and that `import zlib` round-trips. Finally require that a build with nothing
# changed runs no generator and recompiles nothing.
#
# Contract (see tools/test.py): cwd = $REGISTRY_TEST_WORK, $HARBOUR is the
# binary, HOME is an isolated home whose registry list is the checkout under
# test. Exit 0 and print `OK ...` last. $REGISTRY_TEST_OBJECTS is the count
# with the feature ON (the state this script leaves behind); the feature-off
# count is one less (zlibmodule.c is the one extra translation unit).
set -euo pipefail

work="$REGISTRY_TEST_WORK"
test_dir="$REGISTRY_TEST_DIR"
version="$REGISTRY_TEST_UPSTREAM_VERSION"
dep="$work/consumer/.harbour/target/debug/deps/cpython-$REGISTRY_TEST_VERSION"
objects_on="$REGISTRY_TEST_OBJECTS"
objects_off=$((objects_on - 1))

fail() { echo "== FAILED: $*" >&2; exit 1; }

cp -R "$test_dir/consumer" "$work/consumer"
cd "$work/consumer"

build() {
  if ! build_log="$("$HARBOUR" build 2>&1)"; then
    echo "$build_log" | grep -v INFO | tail -40 >&2
    fail "harbour build failed"
  fi
}

count_lines() { grep -c "$1" <<<"$2" || true; }

dep_objects() {
  find "$dep" \( -name probe -o -name host-tools -o -name host-bin \) -prune -o \
    \( -name '*.o' -o -name '*.obj' \) -print | wc -l | tr -d ' '
}

object_present() { [ -n "$(find "$dep/obj" -name "$1" -print -quit)" ]; }

assert_objects() {
  local want=$1 got
  got="$(dep_objects)"
  echo "== compiled $got translation unit(s) (expected $want)"
  [ "$got" = "$want" ] || fail "expected $want translation units, got $got"
}

assert_host_tools() {
  local tool
  for tool in freeze_module bootstrap_python; do
    [ -n "$(find "$work/consumer/.harbour" -path '*/host-bin/*' -name "$tool" -print -quit)" ] \
      || fail "host tool $tool was not built under host-bin/"
    echo "ok   host tool built: $tool"
  done
}

assert_frozen_headers() {
  local count
  count="$(ls "$src"/Python/frozen_modules/*.h | wc -l | tr -d ' ')"
  [ "$count" = 24 ] || fail "bootstrap produced $count frozen headers, expected 24"
  (
    cd "$src/Python/frozen_modules"
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum -c --quiet "$test_dir/expected-frozen.sha256"
    else
      shasum -a 256 -c --quiet "$test_dir/expected-frozen.sha256"
    fi
  ) || fail "a frozen header differs from CPython's own build (the bootstrap ran and wrote it, so a count cannot see this; the digest says it was frozen from the right source)"
  echo "ok   24 frozen headers, byte-identical to CPython's own"
}

run_python() {
  PYTHONHOME=/nonexistent PYTHONPATH="$src/Lib" "$dep/bin/python" "$@"
}

echo "== building cpython (zlib off)"
cp Harbour.toml.off Harbour.toml
t0=$(date +%s)
build
echo "== cold build: $(( $(date +%s) - t0 ))s"

# CPython's own source tree, where the generators write (Python/frozen_modules,
# generated/config.c). Harbour materialises it under the isolated HOME's cache,
# so it can only be found once the first build has fetched it.
src="$(dirname "$(dirname "$(find "$HOME" -path '*cpython/'"$REGISTRY_TEST_VERSION"'/*/Lib/os.py' -print -quit)")")"
[ -f "$src/Lib/os.py" ] || fail "cannot find CPython's source tree under $HOME"

assert_frozen_headers
assert_host_tools
assert_objects "$objects_off"
object_present zlibmodule.o && fail "zlibmodule.o was compiled with the feature off"
echo "ok   no zlibmodule.o with the feature off"

out="$(run_python -c "import sys; print(sys.version.split()[0], sum(i * i for i in range(1000)))")"
echo "$out"
[ "$out" = "$version 332833500" ] || fail "interpreter said '$out'"

if run_python -c "import zlib" 2>zlib-off.err; then
  fail "zlib imported with the feature off"
fi
grep -q "No module named 'zlib'" zlib-off.err || { cat zlib-off.err >&2; fail "expected 'No module named zlib'"; }
echo "ok   zlib absent with the feature off"

echo "== building cpython (zlib on)"
cp Harbour.toml.on Harbour.toml
t0=$(date +%s)
build
log="$build_log"
echo "== rebuild with the feature: $(( $(date +%s) - t0 ))s"
echo "== generators on enabling the feature: ran $(count_lines "Running pre-build step" "$log"), skipped $(count_lines "Skipping pre-build step" "$log")"

grep -q '"zlib", PyInit_zlib' "$src/generated/config.c" \
  || fail "config.c does not list zlib; the generator did not see the feature"
assert_frozen_headers
assert_objects "$objects_on"
object_present zlibmodule.o || fail "zlibmodule.o was not compiled with the feature on"
echo "ok   zlibmodule.o compiled with the feature on"

out="$(run_python -c "
import zlib
data = b'harbour ' * 1000
packed = zlib.compress(data, 9)
assert zlib.decompress(packed) == data
assert len(packed) < len(data) // 20
print('zlib', zlib.ZLIB_VERSION, len(data), '->', len(packed))
")"
echo "$out"
case "$out" in
  "zlib "*" 8000 -> "*) ;;
  *) fail "zlib said '$out'" ;;
esac

echo "== rebuilding cpython (nothing changed)"
t0=$(date +%s)
log="$("$HARBOUR" build 2>&1)"
echo "== no-op build: $(( $(date +%s) - t0 ))s"
skipped="$(count_lines "Skipping pre-build step" "$log")"
if echo "$log" | grep -q "Running pre-build step" || [ "$skipped" -lt 3 ]; then
  echo "$log" | grep -E "pre-build step" >&2
  fail "expected every generator skipped on a build with nothing changed"
fi
if echo "$log" | grep -q "Compiling"; then
  fail "a build with nothing changed recompiled something"
fi
echo "ok   no generator ran ($skipped skipped across the nested builds), nothing recompiled"

echo "OK cpython $version: frozen headers, interpreter, zlib feature, no-op rebuild"
