/*
 * Harbour Registry - Link Test Harness
 *
 * This file is used to verify that a package links correctly.
 * It includes the header and calls a test function to ensure
 * symbols are properly exported.
 *
 * Usage:
 *   cc -I<include_paths> link-test.c -L<lib_paths> -l<libs> -o link-test
 *
 * Template variables (replaced at generation time):
 *   PACKAGE_HEADER - The header file to include
 *   PACKAGE_TEST_CALL - A simple function call to test linkage
 */

#include <PACKAGE_HEADER>

int main(void) {
    /* Call a function to verify linkage */
    PACKAGE_TEST_CALL;
    return 0;
}
