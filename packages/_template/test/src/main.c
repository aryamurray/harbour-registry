/* A consumer test asserts BEHAVIOUR, not that the library links: call into it, check the
 * results, and print `OK ...` as the LAST line of stdout only if everything held.
 * Exit status alone is not enough; every bug this registry's tests exist to catch
 * produced a build that succeeded.
 *
 * The library must also report the version package.toml declares. tools/test.py exports it
 * as REGISTRY_TEST_UPSTREAM_VERSION; this is what catches a version bump whose url or sha256
 * points at the wrong source. Replace the placeholder below with the library's own call
 * (zlibVersion(), cJSON_Version(), ...). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int version_is_declared(const char *what, const char *got) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 0;
    }
    if (strcmp(want, got) != 0) {
        printf("FAIL: %s reports version %s, but package.toml declares %s\n", what, got, want);
        return 0;
    }
    return 1;
}

int main(void) {
    /* TODO: include the library's header and replace both lines. */
    if (!version_is_declared("example_version()", "1.0.0")) return 1;
    printf("OK example 1.0.0: TODO assert something the library actually computes\n");
    return 0;
}
