/* jconfig.h: what upstream's CMake writes from src/jconfig.h.in for the C-only configuration
 * (WITH_SIMD=0, WITH_JPEG7=0, WITH_JPEG8=0, WITH_ARITH_ENC=1, WITH_ARITH_DEC=1). Written by
 * hand for Harbour, because the build runs no CMake. */

/* Version ID for the JPEG library.
 * Might be useful for tests like "#if JPEG_LIB_VERSION >= 60".
 */
#define JPEG_LIB_VERSION  62

/* libjpeg-turbo version */
#define LIBJPEG_TURBO_VERSION  3.2.1

/* libjpeg-turbo version in integer form */
#define LIBJPEG_TURBO_VERSION_NUMBER  3002001

/* Support arithmetic encoding when using 8-bit samples */
#define C_ARITH_CODING_SUPPORTED 1

/* Support arithmetic decoding when using 8-bit samples */
#define D_ARITH_CODING_SUPPORTED 1

/* Support in-memory source/destination managers */
#define MEM_SRCDST_SUPPORTED  1

/* Use accelerated SIMD routines when using 8-bit samples: not built (WITH_SIMD=0). */
/* #undef WITH_SIMD */

/* This version of libjpeg-turbo supports run-time selection of data precision,
 * so BITS_IN_JSAMPLE is no longer used to specify the data precision at build
 * time.  However, some downstream software expects the macro to be defined.
 * Since 12-bit data precision is an opt-in feature that requires explicitly
 * calling 12-bit-specific libjpeg API functions and using 12-bit-specific data
 * types, the unmodified portion of the libjpeg API still behaves as if it were
 * built for 8-bit precision, and JSAMPLE is still literally an 8-bit data
 * type.  Thus, it is correct to define BITS_IN_JSAMPLE to 8 here.
 */
#ifndef BITS_IN_JSAMPLE
#define BITS_IN_JSAMPLE  8
#endif

#ifdef _WIN32

#undef RIGHT_SHIFT_IS_UNSIGNED

/* Define "boolean" as unsigned char, not int, per Windows custom */
#ifndef __RPCNDR_H__            /* don't conflict if rpcndr.h already read */
typedef unsigned char boolean;
#endif
#define HAVE_BOOLEAN            /* prevent jmorecfg.h from redefining it */

/* Define "INT32" as int, not long, per Windows custom */
#if !(defined(_BASETSD_H_) || defined(_BASETSD_H))   /* don't conflict if basetsd.h already read */
typedef short INT16;
typedef signed int INT32;
#endif
#define XMD_H                   /* prevent jmorecfg.h from redefining it */

#else

/* RIGHT_SHIFT_IS_UNSIGNED: CMake's try_run (cmakescripts/right_shift_is_unsigned.c) answers
 * "signed" on every supported compiler, so it stays undefined. */
/* #undef RIGHT_SHIFT_IS_UNSIGNED */

#endif
