#!/usr/bin/env bash
# Redis through the registry: build the server, CLI and benchmark as a
# dependency, then run them against each other. A custom driver rather than a
# consumer program, because redis is not a library anyone links: the behaviour
# worth asserting is a server answering commands.
#
# Contract (see tools/test.py): cwd = $REGISTRY_TEST_WORK, $HARBOUR is the
# binary, HOME already points at an isolated home whose registry list is the
# checkout under test. Exit 0 and print `OK ...` last.
set -euo pipefail

work="$REGISTRY_TEST_WORK"
version="$REGISTRY_TEST_UPSTREAM_VERSION"

fail() {
  echo "== FAILED: $*" >&2
  echo "-- server log --" >&2
  cat "$work/data/redis.log" >&2 2>/dev/null || true
  exit 1
}

case "$(uname -s)" in
  Linux) loop=epoll ;;
  Darwin) loop=kqueue ;;
  *) fail "redis has no port for $(uname -s)" ;;
esac

cp -R "$REGISTRY_TEST_DIR/consumer" "$work/consumer"
(cd "$work/consumer" && "$HARBOUR" build)

bin="$work/consumer/.harbour/target/debug/deps/redis-$REGISTRY_TEST_VERSION/bin"
for exe in redis-server redis-cli redis-benchmark; do
  [ -x "$bin/$exe" ] || fail "$exe was not built under $bin"
done

# A free port chosen by the kernel, so parallel jobs on one runner cannot collide.
port="$("$REGISTRY_TEST_PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
data="$work/data"
mkdir -p "$data"

cli() { "$bin/redis-cli" -h 127.0.0.1 -p "$port" "$@"; }

expect() {
  local want=$1 got
  shift
  got="$(cli "$@")" || fail "'$*' exited non-zero (got: $got)"
  if [ "$got" != "$want" ]; then
    fail "'$*' replied '$got', expected '$want'"
  fi
  echo "ok   $* -> $want"
}

echo "== redis-server --version"
version_line="$("$bin/redis-server" --version)"
echo "$version_line"
case "$version_line" in
  *"v=$version "*) ;;
  *) fail "expected version $version in: $version_line" ;;
esac
# Built with libc malloc, not the bundled jemalloc upstream defaults to on Linux.
case "$version_line" in
  *"malloc=libc"*) ;;
  *) fail "expected malloc=libc in: $version_line" ;;
esac
case "$("$bin/redis-cli" --version)" in
  "redis-cli $version") ;;
  *) fail "redis-cli did not report $version" ;;
esac

echo "== starting redis-server on port $port"
"$bin/redis-server" --port "$port" --bind 127.0.0.1 --dir "$data" \
  --save "" --appendonly no --logfile "$data/redis.log" &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT

ready=0
for _ in $(seq 1 100); do
  if [ "$(cli ping 2>/dev/null)" = "PONG" ]; then ready=1; break; fi
  kill -0 "$server_pid" 2>/dev/null || fail "the server exited before it answered"
  sleep 0.1
done
[ "$ready" -eq 1 ] || fail "the server did not answer PING within 10 s"

expect PONG ping
expect OK set canary value
expect value get canary
expect 1 incr counter
expect 2 incr counter

got="$(cli info server | tr -d '\r' | sed -n 's/^multiplexing_api://p')"
[ "$got" = "$loop" ] || fail "expected the $loop event loop, INFO reports '$got'"
echo "ok   event loop: $got"

# Lua and the modules built from deps/: each is a separate translation unit set.
expect 3 eval "return redis.call('incr', 'counter')" 0
expect "[1,2,3]" eval "return cjson.encode({1,2,3})" 0
expect 7 eval "return cmsgpack.unpack(cmsgpack.pack({7}))[1]" 0
expect 8 eval "return bit.band(12, 10)" 0
expect 258 eval "return struct.unpack('>H', struct.pack('>H', 258))" 0

echo "== redis-benchmark (links hiredis and hdr_histogram)"
bench="$("$bin/redis-benchmark" -h 127.0.0.1 -p "$port" -q -n 2000 -c 5 -t ping_mbulk,set 2>&1)" \
  || fail "redis-benchmark exited non-zero: $bench"
echo "$bench" | tr '\r' '\n' | tail -3
case "$bench" in
  *"requests per second"*) ;;
  *) fail "redis-benchmark did not report a rate: $bench" ;;
esac

echo "== shutting down"
cli shutdown nosave >/dev/null 2>&1 || true
set +e
for _ in $(seq 1 50); do
  kill -0 "$server_pid" 2>/dev/null || break
  sleep 0.1
done
if kill -0 "$server_pid" 2>/dev/null; then
  fail "the server was still running 5 s after SHUTDOWN"
fi
wait "$server_pid"
status=$?
set -e
[ "$status" -eq 0 ] || fail "the server exited with status $status after SHUTDOWN"
trap - EXIT
echo "OK redis $version: server, CLI, Lua modules and benchmark"
