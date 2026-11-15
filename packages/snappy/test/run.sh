#!/usr/bin/env bash
# snappy through the registry: build the consumer, run it, then assert what no output can show.
#
# A plain consumer cannot be the whole test here. snappy's `config.h` is made of `#cmakedefine01` macros
# the sources test with `#if`, and an undefined macro is 0, so a manifest that answers none of upstream's
# questions still compiles and still round-trips every byte, on fallback code that skips
# `__builtin_expect`, prefetch and the count-trailing-zeros intrinsic. The decompressed bytes cannot tell
# the two builds apart, so this reads the compile command Harbour recorded for `snappy.cc` instead.
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

# The compile command Harbour recorded for the package's `snappy.cc`, one line.
cmd="$("$REGISTRY_TEST_PYTHON" -I - .harbour/compile_commands.json <<'PY'
import json, sys
for entry in json.load(open(sys.argv[1])):
    if entry["file"].replace("\\", "/").endswith("/snappy.cc"):
        print(entry.get("command") or " ".join(entry["arguments"]))
        break
PY
)"
[ -n "$cmd" ] || fail "no compile command recorded for snappy.cc"

if [ "${REGISTRY_TEST_PLATFORM:-}" = "windows" ]; then
  # cl: none of the gcc/clang answers may leak in (HAVE_BUILTIN_* name builtins cl lacks), and none of the
  # POSIX headers exist, so the probes must have answered no. No ISA extension is claimed either.
  case " $cmd " in
    *HAVE_BUILTIN_EXPECT* | *HAVE_BUILTIN_CTZ* | *HAVE_BUILTIN_PREFETCH* | *HAVE_ATTRIBUTE_ALWAYS_INLINE* | \
      *HAVE_SYS_MMAN_H=1* | *HAVE_UNISTD_H=1* | *HAVE_FUNC_MMAP=1* | *SNAPPY_HAVE_SSSE3* | *SNAPPY_HAVE_BMI2* | \
      *SNAPPY_HAVE_X86_CRC32* | *SNAPPY_HAVE_NEON*)
      fail "the windows build claims a gcc/clang or POSIX fact. command: $cmd" ;;
    *) echo "ok   snappy.cc is compiled with no gcc/clang builtin or POSIX answer" ;;
  esac
else
  # Every fact the manifest asserts about the compiler and the platform must reach the compiler.
  for want in HAVE_BUILTIN_EXPECT=1 HAVE_BUILTIN_CTZ=1 HAVE_BUILTIN_PREFETCH=1 \
    HAVE_ATTRIBUTE_ALWAYS_INLINE=1 HAVE_FUNC_MMAP=1 HAVE_FUNC_SYSCONF=1 \
    HAVE_SYS_MMAN_H=1 HAVE_UNISTD_H=1; do
    case " $cmd " in
      *" -D$want "*) echo "ok   snappy.cc is compiled with -D$want" ;;
      *) fail "snappy.cc is not compiled with -D$want: the library still builds and round-trips, on the portable fallback for whatever this macro selects. command: $cmd" ;;
    esac
  done

  # NEON is the aarch64 baseline; x86_64 must NOT claim an ISA the manifest passes no -m flag for, or the
  # binary would fault on an older CPU.
  case "$(uname -m)" in
    arm64 | aarch64)
      case " $cmd " in
        *" -DSNAPPY_HAVE_NEON=1 "*) echo "ok   snappy.cc is compiled with -DSNAPPY_HAVE_NEON=1" ;;
        *) fail "aarch64 build lacks -DSNAPPY_HAVE_NEON=1. command: $cmd" ;;
      esac
      ;;
    *)
      case " $cmd " in
        *" -DSNAPPY_HAVE_SSSE3"* | *" -DSNAPPY_HAVE_BMI2"* | *" -DSNAPPY_HAVE_X86_CRC32"* | *" -DSNAPPY_HAVE_NEON"*)
          fail "the build claims an ISA feature the manifest passes no flag for. command: $cmd" ;;
        *) echo "ok   snappy.cc claims no ISA extension beyond the baseline" ;;
      esac
      ;;
  esac

fi

exe="$(find .harbour/target \( -path '*/bin/snappy-test' -o -path '*/bin/snappy-test.exe' \) -print -quit)"
[ -x "$exe" ] || fail "the consumer executable was not built"
"$exe"
