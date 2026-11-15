#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Hot loop long enough for the tracing JIT to compile it, then ask the VM
   whether it did. Correct arithmetic alone proves only the interpreter; the
   generated assembly VM and the JIT backend are what this is here to hit. */
static const char *script =
  "local s = 0\n"
  "for i = 1, 1e6 do s = s + i % 7 end\n"
  "local ffi = require('ffi')\n"
  "ffi.cdef[[ int abs(int); ]]\n"
  "local st = {jit.status()}\n"
  "return s, ffi.C.abs(-42), st[1], jit.arch, jit.version, _VERSION\n";

/* The architecture name LuaJIT's `jit.arch` reports. A build that picked the wrong DynASM flags crashes
   rather than printing the wrong name, but a manifest block selected for the wrong platform should
   name itself. */
#if defined(__x86_64__) || defined(_M_X64)
#define EXPECT_ARCH "x64"
#elif defined(__aarch64__) || defined(_M_ARM64)
#define EXPECT_ARCH "arm64"
#else
#error "no expected jit.arch for this architecture"
#endif

int main(void) {
    lua_State *L = luaL_newstate();
    if (!L) { puts("FAIL: no state"); return 1; }
    luaL_openlibs(L);
    if (luaL_dostring(L, script)) { printf("FAIL: %s\n", lua_tostring(L, -1)); return 1; }
    double s = lua_tonumber(L, -6);
    int a = (int)lua_tonumber(L, -5);
    int jit_on = lua_toboolean(L, -4);
    const char *arch = lua_tostring(L, -3);
    const char *jit_version = lua_tostring(L, -2);
    const char *lua_version = lua_tostring(L, -1);
    if (s != 2999998) { printf("FAIL: loop sum %.0f\n", s); return 1; }
    if (a != 42) { printf("FAIL: ffi abs %d\n", a); return 1; }
    if (!jit_on) { puts("FAIL: JIT not enabled"); return 1; }
    if (strcmp(EXPECT_ARCH, arch) != 0) {
        printf("FAIL: jit.arch is %s, expected %s\n", arch, EXPECT_ARCH);
        return 1;
    }
    if (strcmp(lua_version, "Lua 5.1") != 0) {
        printf("FAIL: _VERSION is %s\n", lua_version);
        return 1;
    }
    /* tools/test.py exports the upstream version that package.toml declares; the VM must report it
       ("LuaJIT 2.1.<commit timestamp>"). */
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (want == NULL || *want == '\0') {
        puts("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)");
        return 1;
    }
    char expected[64];
    snprintf(expected, sizeof expected, "LuaJIT %s", want);
    if (strcmp(jit_version, expected) != 0) {
        printf("FAIL: jit.version is '%s', but package.toml declares '%s'\n", jit_version, expected);
        return 1;
    }
    printf("OK %s loop=%.0f ffi=%d jit=on arch=%s\n", jit_version, s, a, arch);
    lua_close(L);
    return 0;
}
