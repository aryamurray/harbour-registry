/* config.h for Harbour: what upstream's CMake writes from src/config-cmake.h.in for its
 * default options plus PCRE2_BUILD_PCRE2_8=ON only (PCRE2_SUPPORT_UNICODE=ON,
 * PCRE2_SUPPORT_JIT=OFF, PCRE2_NEWLINE=LF, PCRE2_LINK_SIZE=2, no readline/libz/libbz2).
 * The check_c_source_compiles answers are `#if`s on the compiler. */

#if defined(__has_include)
#if __has_include(<assert.h>)
#define HAVE_ASSERT_H 1
#endif
#else
#define HAVE_ASSERT_H 1
#endif

#ifdef _MSC_VER
/* __assume(1) compiles only on MSVC */
#define HAVE_BUILTIN_ASSUME 1
#else
#define HAVE_BUILTIN_MUL_OVERFLOW 1
#define HAVE_BUILTIN_UNREACHABLE 1
#endif

#if defined(__has_attribute)
#if __has_attribute(uninitialized)
#define HAVE_ATTRIBUTE_UNINITIALIZED 1
#endif
#endif

/* HAVE_DIRENT_H, HAVE_SYS_STAT_H, HAVE_SYS_TYPES_H, HAVE_UNISTD_H, HAVE_WINDOWS_H,
   HAVE_MEMFD_CREATE, HAVE_SECURE_GETENV and HAVE_SETRLIMIT are read only by pcre2test,
   pcre2grep and the JIT (pcre2_jit_*.c, which this configuration compiles as stubs). */

#define SUPPORT_PCRE2_8 1
/* #undef SUPPORT_PCRE2_16 */
/* #undef SUPPORT_PCRE2_32 */
/* #undef DISABLE_PERCENT_ZT */

/* #undef SUPPORT_JIT */
/* #undef SLJIT_PROT_EXECUTABLE_ALLOCATOR */
#define SUPPORT_UNICODE 1
/* #undef SUPPORT_VALGRIND */

/* #undef BSR_ANYCRLF */
/* #undef EBCDIC */
/* #undef EBCDIC_NL25 */
/* #undef EBCDIC_IGNORING_COMPILER */
/* #undef NEVER_BACKSLASH_C */

#ifdef _MSC_VER
#define PCRE2_EXPORT
#else
#define PCRE2_EXPORT            __attribute__ ((visibility ("default")))
#endif
#define LINK_SIZE               2
#define HEAP_LIMIT              20000000
#define MATCH_LIMIT             10000000
#define MATCH_LIMIT_DEPTH       MATCH_LIMIT
#define MAX_VARLOOKBEHIND       255
#define NEWLINE_DEFAULT         2
#define PARENS_NEST_LIMIT       250
#define PCRE2GREP_BUFSIZE       20480
#define PCRE2GREP_MAX_BUFSIZE   1048576

#define MAX_NAME_SIZE           128
#define MAX_NAME_COUNT          10000

/* end config.h for CMake builds */
