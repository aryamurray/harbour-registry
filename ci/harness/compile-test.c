/*
 * Harbour Registry - Compile Test Harness
 *
 * This file is used to verify that a package's headers can be included.
 * It is generated from the package's [metadata.harness] configuration.
 *
 * Usage:
 *   cc -I<include_paths> compile-test.c -c -o compile-test.o
 *
 * Template variables (replaced at generation time):
 *   PACKAGE_HEADER - The header file to include
 */

#include <PACKAGE_HEADER>

int main(void) {
    /* Just verify compilation succeeds */
    return 0;
}
