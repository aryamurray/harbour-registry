/* config.h for Harbour: what upstream's CMake writes from cmake/config.h.in (HAVE_CONFIG_H is
 * defined by CMakeLists.txt: add_definitions(-DHAVE_CONFIG_H)) for its default options, minus
 * everything only the command-line tools read (GIF/JPEG/PNG/TIFF/SDL/GL/WIC). The compiler
 * probes of cmake/deps.cmake and cmake/cpu.cmake are `#if`s on the compiler and architecture. */

/* Set to 1 if __builtin_bswap16/32/64 are available (deps.cmake: check_c_source_compiles). */
#if defined(__GNUC__) || defined(__clang__)
#define HAVE_BUILTIN_BSWAP16 1
#define HAVE_BUILTIN_BSWAP32 1
#define HAVE_BUILTIN_BSWAP64 1
#endif

#ifdef _WIN32
#define HAVE_WINDOWS_H 1
#define HAVE_SHLWAPI_H 1
#else
#define HAVE_UNISTD_H 1
#endif

/* Enable near lossless encoding (WEBP_NEAR_LOSSLESS=ON) */
#define WEBP_NEAR_LOSSLESS 1

/* Threading: WEBP_USE_THREAD=ON and Threads found (pthreads, or the Win32 threads of
 * thread_utils.c). */
#define WEBP_USE_THREAD 1

/* SIMD (cmake/cpu.cmake, WEBP_ENABLE_SIMD=ON): which instruction sets the build has files for.
 * x86-64: SSE2 is the baseline, SSE4.1 and AVX2 files are built with their own flags (targets
 * webp_sse41 and webp_avx2); the code selects by CPUID at run time. 64-bit Arm: NEON is the
 * baseline. Other architectures get the portable C. */
#if defined(__x86_64__) || defined(_M_X64)
#define WEBP_HAVE_SSE2 1
#define WEBP_HAVE_SSE41 1
#define WEBP_HAVE_AVX2 1
#elif defined(__aarch64__) || defined(_M_ARM64)
#define WEBP_HAVE_NEON 1
#endif

/* Define WORDS_BIGENDIAN to 1 if your processor stores words with the most
   significant byte first. Not defined: every supported target is little endian. */
