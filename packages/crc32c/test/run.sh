#!/usr/bin/env bash
# crc32c through the registry: build the consumer, run it, then assert which object holds the accelerated code.
#
# A plain consumer cannot be the whole test here. Upstream keeps `crc32c_sse42.cc` and `crc32c_arm64.cc`
# apart from the portable code because each needs an ISA flag the portable code must not get, and Harbour
# has no per-source flags, so the manifest makes each its own `staticlib` target joined with `local = true`.
# The portable and accelerated paths compute the same function, so a build whose `arch` blocks never
# matched (every `HAVE_*` define gone at once) still returns every correct checksum, just slowly. The only
# witness is which object defines the accelerated entry point and which one is an empty unit.
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

dep="$work/consumer/.harbour/target/debug/deps/crc32c-$REGISTRY_TEST_VERSION"
[ -d "$dep" ] || fail "no built dependency directory $dep"
# Windows (cl) writes COFF .obj files whose symbols are MSVC-mangled and which `nm` may not read, so there
# the witness is the mangled name's presence in the object file (see the windows branch below).
win=0
[ "${REGISTRY_TEST_PLATFORM:-}" = "windows" ] && win=1
ext=o
[ "$win" = 1 ] && ext=obj
[ "$win" = 1 ] || command -v nm >/dev/null 2>&1 || fail "\`nm\` is not on PATH, so the symbol-level checks cannot run (install binutils)"

find_object() { find "$dep" -type d \( -name host-tools -o -name host-bin -o -name probe \) -prune -o -name "$1" -print -quit; }

# `nm` output for one object, captured rather than piped: under `set -o pipefail`, `grep -q` exits at the
# first match while `nm` is still writing, and the pipeline then fails after the symbol was found.
symbols_of() {
  local obj
  obj="$(find_object "$1")"
  [ -n "$obj" ] || fail "expected object \`$1\` was not compiled"
  nm "$obj" 2>/dev/null
}

# Mach-O prefixes C symbols with `_`, ELF does not.
defines() {
  local syms
  syms="$(symbols_of "$1")"
  grep -qE " [TtDdSsRr] _?$2\$" <<<"$syms" || fail "\`$1\` does not define \`$2\`: the object exists but its code was compiled away behind an #if"
  echo "ok   $1 defines $2"
}
lacks() {
  local syms
  syms="$(symbols_of "$1")"
  if grep -qE " _?$2\$" <<<"$syms"; then fail "\`$1\` mentions \`$2\` on an architecture that has no such implementation"; fi
  echo "ok   $1 does not mention $2"
}
references() {
  local syms
  syms="$(symbols_of "$1")"
  grep -qE " U _?$2\$" <<<"$syms" || fail "\`$1\` has no undefined reference to \`$2\`: it is not dispatching to the accelerated implementation"
  echo "ok   $1 references $2 (dispatches to it)"
}

SSE42_SYM='_ZN6crc32c11ExtendSse42EjPKhm'
ARM64_SYM='_ZN6crc32c11ExtendArm64EjPKhm'

# On each architecture exactly one accelerated object holds code and the other is an empty translation
# unit. Both are asserted: the live one defines its entry point, and the dead one does not, which is what
# catches an `arch` block that matches the wrong machine (an ISA flag leaking onto a target that runs on
# CPUs without it).
for o in crc32c_sse42.$ext crc32c_arm64.$ext; do
  [ -n "$(find_object "$o")" ] || fail "expected object \`$o\` was not compiled"
  echo "ok   object present: $o"
done
if [ "$win" = 1 ]; then
  # An MSVC object keeps the (mangled) name of every function it defines or calls in its symbol table, so
  # the name's presence is the witness: the live ISA object defines its entry point, the dead one has
  # nothing to name, and the dispatcher calls it. x86_64 only: cl has no ARM CRC32C flag (upstream's
  # CMakeLists.txt: "TODO: insert correct flag when VS gets ARM64 CRC32C support").
  mentions() { grep -aq "$2" "$(find_object "$1")"; }
  mentions crc32c_sse42.obj ExtendSse42 || fail "crc32c_sse42.obj does not define ExtendSse42: its code was compiled away behind an #if"
  echo "ok   crc32c_sse42.obj defines ExtendSse42"
  if mentions crc32c_arm64.obj ExtendArm64; then fail "crc32c_arm64.obj mentions ExtendArm64 on x86_64"; fi
  echo "ok   crc32c_arm64.obj does not mention ExtendArm64"
  mentions crc32c.obj ExtendSse42 || fail "crc32c.obj has no reference to ExtendSse42: it is not dispatching to the accelerated implementation"
  echo "ok   crc32c.obj references ExtendSse42 (dispatches to it)"
else
  case "$(uname -m)" in
    x86_64 | amd64)
      defines crc32c_sse42.$ext "$SSE42_SYM"
      lacks crc32c_arm64.$ext "$ARM64_SYM"
      references crc32c.$ext "$SSE42_SYM"
      ;;
    arm64 | aarch64)
      defines crc32c_arm64.$ext "$ARM64_SYM"
      lacks crc32c_sse42.$ext "$SSE42_SYM"
      references crc32c.$ext "$ARM64_SYM"
      ;;
    *) fail "no expected accelerated object for $(uname -m)" ;;
  esac

fi

# `HAVE_STRONG_GETAUXVAL` is the one answer that really differs per OS, and it is a *measured* one
# (`probes.named`). On aarch64 Linux it is what lets `CanUseArm64Linux()` ask the kernel whether the CPU
# has CRC32 instructions; without it that function returns false, the accelerated object is linked and
# never called, and every checksum is still correct. macOS has no <sys/auxv.h>, so there the macro must
# be *absent*: a probe that answered yes on a Mac would be as wrong as one that answered no on Linux.
cmd="$("$REGISTRY_TEST_PYTHON" -I - .harbour/compile_commands.json <<'PY'
import json, sys
for entry in json.load(open(sys.argv[1])):
    if entry["file"].replace("\\", "/").endswith("/crc32c.cc"):
        print(entry.get("command") or " ".join(entry["arguments"]))
        break
PY
)"
[ -n "$cmd" ] || fail "no compile command recorded for crc32c.cc"
case "$(uname -s)-$(uname -m)" in
  Linux-aarch64 | Linux-arm64)
    case " $cmd " in
      *" -DHAVE_STRONG_GETAUXVAL=1 "*) echo "ok   crc32c.cc is compiled with -DHAVE_STRONG_GETAUXVAL=1" ;;
      *) fail "aarch64 Linux build lacks -DHAVE_STRONG_GETAUXVAL=1; the accelerated CRC would be linked and never called. command: $cmd" ;;
    esac
    ;;
  Darwin-*)
    case " $cmd " in
      *"HAVE_STRONG_GETAUXVAL"*) fail "a macOS build claims getauxval, which it does not have" ;;
      *) echo "ok   crc32c.cc does not claim getauxval on macOS" ;;
    esac
    ;;
esac

exe="$(find .harbour/target \( -path '*/bin/crc32c-test' -o -path '*/bin/crc32c-test.exe' \) -print -quit)"
[ -x "$exe" ] || fail "the consumer executable was not built"
"$exe"
