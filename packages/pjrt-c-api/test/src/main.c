/* Consumer test for pjrt-c-api (PJRT C API of OpenXLA), through the registry.
 *
 * Plain C, no plugin is loaded and no API function is called: the headers are the product, so this checks
 * that the right headers were found (the API version, struct layout rules the header defines, the member
 * names a client assigns, the documented enum numbering) and that every public extension header compiles
 * together as C. It is written to compile with GCC, Clang and MSVC (no statement expressions, no
 * _Static_assert, no variable-length arrays, no C23). */
#include "xla/pjrt/c/pjrt_c_api.h"
#include "xla/pjrt/c/pjrt_c_api_abi_version_extension.h"
#include "xla/pjrt/c/pjrt_c_api_custom_partitioner_extension.h"
#include "xla/pjrt/c/pjrt_c_api_device_event.h"
#include "xla/pjrt/c/pjrt_c_api_ffi_extension.h"
#include "xla/pjrt/c/pjrt_c_api_gpu_extension.h"
#include "xla/pjrt/c/pjrt_c_api_layouts_extension.h"
#include "xla/pjrt/c/pjrt_c_api_macros.h"
#include "xla/pjrt/c/pjrt_c_api_memory_descriptions_extension.h"
#include "xla/pjrt/c/pjrt_c_api_multi_slice_extension.h"
#include "xla/pjrt/c/pjrt_c_api_phase_compile_extension.h"
#include "xla/pjrt/c/pjrt_c_api_profiler_extension.h"
#include "xla/pjrt/c/pjrt_c_api_raw_buffer_extension.h"
#include "xla/pjrt/c/pjrt_c_api_raw_client.h"
#include "xla/pjrt/c/pjrt_c_api_shardings_extension.h"
#include "xla/pjrt/c/pjrt_c_api_stream_extension.h"
#include "xla/pjrt/c/pjrt_c_api_tpu_executable_extension.h"
#include "xla/pjrt/c/pjrt_c_api_tpu_topology_extension.h"
#include "xla/pjrt/c/pjrt_c_api_xla_transform_extension.h"

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Values recorded for this release (xla commit 45c7aaa, pjrt_c_api.h). */
#define WANT_MAJOR 0
#define WANT_MINOR 118
#define WANT_API_FUNCTIONS 138

static int failures = 0;
#define CHECK(cond)                                                    \
    do {                                                               \
        if (!(cond)) {                                                 \
            printf("FAIL line %d: %s\n", __LINE__, #cond);             \
            failures++;                                                \
        }                                                              \
    } while (0)

/* The header's rule: <T>_STRUCT_SIZE is offsetof(last_field) + sizeof(last_field), struct_size is the
 * first member, extension_start the second. */
#define CHECK_ARGS(T, last)                                                              \
    do {                                                                                 \
        CHECK(offsetof(T, struct_size) == 0);                                            \
        CHECK(sizeof(((T *)0)->struct_size) == sizeof(size_t));                          \
        CHECK(offsetof(T, extension_start) == sizeof(size_t));                           \
        CHECK((size_t)T##_STRUCT_SIZE == offsetof(T, last) + sizeof(((T *)0)->last));    \
        CHECK(sizeof(T) >= (size_t)T##_STRUCT_SIZE);                                     \
        CHECK(sizeof(T) - (size_t)T##_STRUCT_SIZE < sizeof(void *)); /* only tail padding */ \
    } while (0)

/* Some args structs of the extensions have no extension_start: struct_size is still first. */
#define CHECK_ARGS_NOEXT(T, last)                                                        \
    do {                                                                                 \
        CHECK(offsetof(T, struct_size) == 0);                                            \
        CHECK((size_t)T##_STRUCT_SIZE == offsetof(T, last) + sizeof(((T *)0)->last));    \
        CHECK(sizeof(T) >= (size_t)T##_STRUCT_SIZE);                                     \
    } while (0)

/* An extension struct starts with PJRT_Extension_Base. */
#define CHECK_EXT(T, last)                                                               \
    do {                                                                                 \
        CHECK(offsetof(T, base) == 0);                                                   \
        CHECK((size_t)T##_STRUCT_SIZE == offsetof(T, last) + sizeof(((T *)0)->last));    \
        CHECK(sizeof(T) >= (size_t)T##_STRUCT_SIZE);                                     \
    } while (0)

PJRT_CAPI_EXPORT int exported_probe(void) { return 42; }

int main(void) {
    char version[32];
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (!want || !*want) {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 1;
    }

    /* 1. API version: the macros are what the release's version is derived from. */
    snprintf(version, sizeof version, "%d.%d", PJRT_API_MAJOR, PJRT_API_MINOR);
    if (strcmp(version, want) != 0) {
        printf("FAIL: header declares PJRT API %s, expected %s\n", version, want);
        return 1;
    }
    CHECK(PJRT_API_MAJOR == WANT_MAJOR);
    CHECK(PJRT_API_MINOR == WANT_MINOR);

    /* 2. Versioned-struct rules, on structs from several headers. */
    CHECK(offsetof(PJRT_Extension_Base, struct_size) == 0);
    CHECK(offsetof(PJRT_Extension_Base, type) >= sizeof(size_t));
    CHECK((size_t)PJRT_Extension_Base_STRUCT_SIZE ==
          offsetof(PJRT_Extension_Base, next) + sizeof(void *));
    CHECK(offsetof(PJRT_Extension_Base, next) > offsetof(PJRT_Extension_Base, type));
    CHECK_ARGS(PJRT_Api_Version, minor_version);
    CHECK_ARGS(PJRT_Error_Destroy_Args, error);
    CHECK_ARGS(PJRT_Error_Message_Args, message_size);
    CHECK_ARGS(PJRT_Client_Create_Args, kv_try_get_user_arg);
    CHECK_ARGS(PJRT_Compile_Args, executable);
    CHECK_ARGS_NOEXT(PJRT_Gpu_Register_Custom_Call_Args, handler_record);
    CHECK_ARGS(PJRT_Layouts_MemoryLayout_Destroy_Args, layout);
    CHECK_ARGS_NOEXT(PJRT_FFI_Register_Handler_Args, traits);
    CHECK_EXT(PJRT_Stream_Extension, wait_stream);
    CHECK_EXT(PJRT_Layouts_Extension, PJRT_Layouts_PJRT_Executable_GetParameterLayouts);
    CHECK_EXT(PJRT_FFI_Extension, register_handler);
    CHECK_EXT(PJRT_Profiler_Extension, traceme_context_id);
    CHECK_EXT(PJRT_Gpu_Custom_Call, custom_call_handles_record);
    CHECK(PJRT_Api_Version_STRUCT_SIZE == offsetof(PJRT_Api_Version, minor_version) + sizeof(int));

    /* 3. PJRT_Api: header-defined size, leading members, and every member a client needs, by name.
     *    Assigning NULL to a member by name fails to compile if upstream renames it. */
    {
        PJRT_Api api;
        PJRT_Layouts_Extension layouts;
        PJRT_Stream_Extension stream;
        size_t first = offsetof(PJRT_Api, PJRT_Error_Destroy);
        size_t last = offsetof(PJRT_Api, PJRT_TopologyDescription_GetMemorySpaceKindIds);
        size_t nfuncs;

        memset(&api, 0xff, sizeof api);
        api.struct_size = PJRT_Api_STRUCT_SIZE;
        api.extension_start = NULL;
        api.pjrt_api_version.struct_size = PJRT_Api_Version_STRUCT_SIZE;
        api.pjrt_api_version.extension_start = NULL;
        api.pjrt_api_version.major_version = PJRT_API_MAJOR;
        api.pjrt_api_version.minor_version = PJRT_API_MINOR;
        api.PJRT_Error_Destroy = NULL;
        api.PJRT_Error_Message = NULL;
        api.PJRT_Error_GetCode = NULL;
        api.PJRT_Plugin_Initialize = NULL;
        api.PJRT_Plugin_Attributes = NULL;
        api.PJRT_Event_Destroy = NULL;
        api.PJRT_Event_IsReady = NULL;
        api.PJRT_Event_Error = NULL;
        api.PJRT_Event_Await = NULL;
        api.PJRT_Event_OnReady = NULL;
        api.PJRT_Client_Create = NULL;
        api.PJRT_Client_Destroy = NULL;
        api.PJRT_Client_PlatformName = NULL;
        api.PJRT_Client_Devices = NULL;
        api.PJRT_Client_AddressableDevices = NULL;
        api.PJRT_Client_Compile = NULL;
        api.PJRT_Client_BufferFromHostBuffer = NULL;
        api.PJRT_DeviceDescription_Id = NULL;
        api.PJRT_DeviceDescription_Kind = NULL;
        api.PJRT_Device_GetDescription = NULL;
        api.PJRT_Executable_Name = NULL;
        api.PJRT_Executable_Destroy = NULL;
        api.PJRT_LoadedExecutable_Execute = NULL;
        api.PJRT_LoadedExecutable_Destroy = NULL;
        api.PJRT_Buffer_Destroy = NULL;
        api.PJRT_Buffer_ToHostBuffer = NULL;
        api.PJRT_Buffer_ElementType = NULL;
        api.PJRT_Buffer_Dimensions = NULL;
        api.PJRT_Compile = NULL;
        api.PJRT_TopologyDescription_Fingerprint = NULL;

        CHECK(offsetof(PJRT_Api, struct_size) == 0);
        CHECK(offsetof(PJRT_Api, extension_start) == sizeof(size_t));
        CHECK(offsetof(PJRT_Api, pjrt_api_version) > offsetof(PJRT_Api, extension_start));
        CHECK(first >= offsetof(PJRT_Api, pjrt_api_version) + sizeof(PJRT_Api_Version));
        CHECK(offsetof(PJRT_Api, PJRT_Error_Message) == first + sizeof(void *));
        CHECK((size_t)PJRT_Api_STRUCT_SIZE == last + sizeof(api.PJRT_TopologyDescription_GetMemorySpaceKindIds));
        CHECK(sizeof(PJRT_Api) == (size_t)PJRT_Api_STRUCT_SIZE);
        CHECK(sizeof(api.PJRT_Client_Create) == sizeof(void (*)(void)));
        /* Every member from PJRT_Error_Destroy to the last one is a function pointer, one per slot. */
        nfuncs = (last - first) / sizeof(void *) + 1;
        CHECK((last - first) % sizeof(void *) == 0);
        CHECK(nfuncs == WANT_API_FUNCTIONS);
        CHECK(api.pjrt_api_version.major_version == WANT_MAJOR);
        CHECK(api.pjrt_api_version.minor_version == WANT_MINOR);

        /* Extension chain: base.type values and a chain walk over structs from two headers. */
        memset(&layouts, 0, sizeof layouts);
        memset(&stream, 0, sizeof stream);
        layouts.base.struct_size = PJRT_Layouts_Extension_STRUCT_SIZE;
        layouts.base.type = PJRT_Extension_Type_Layouts;
        layouts.base.next = &stream.base;
        layouts.PJRT_Layouts_MemoryLayout_Destroy = NULL;
        layouts.PJRT_Layouts_MemoryLayout_Serialize = NULL;
        stream.base.struct_size = PJRT_Stream_Extension_STRUCT_SIZE;
        stream.base.type = PJRT_Extension_Type_Stream;
        stream.base.next = NULL;
        stream.get_stream = NULL;
        stream.wait_stream = NULL;
        api.extension_start = &layouts.base;
        {
            const PJRT_Extension_Base *e = api.extension_start;
            int n = 0, seen_stream = 0;
            while (e) {
                n++;
                if (e->type == PJRT_Extension_Type_Stream) seen_stream = 1;
                e = e->next;
            }
            CHECK(n == 2);
            CHECK(seen_stream);
        }
    }

    /* 4. Documented numbering (the codes follow absl::StatusCode; the extension types are ABI). */
    CHECK(PJRT_Error_Code_OK == 0);
    CHECK(PJRT_Error_Code_CANCELLED == 1);
    CHECK(PJRT_Error_Code_UNKNOWN == 2);
    CHECK(PJRT_Error_Code_INVALID_ARGUMENT == 3);
    CHECK(PJRT_Error_Code_DEADLINE_EXCEEDED == 4);
    CHECK(PJRT_Error_Code_NOT_FOUND == 5);
    CHECK(PJRT_Error_Code_ALREADY_EXISTS == 6);
    CHECK(PJRT_Error_Code_PERMISSION_DENIED == 7);
    CHECK(PJRT_Error_Code_RESOURCE_EXHAUSTED == 8);
    CHECK(PJRT_Error_Code_FAILED_PRECONDITION == 9);
    CHECK(PJRT_Error_Code_ABORTED == 10);
    CHECK(PJRT_Error_Code_OUT_OF_RANGE == 11);
    CHECK(PJRT_Error_Code_UNIMPLEMENTED == 12);
    CHECK(PJRT_Error_Code_INTERNAL == 13);
    CHECK(PJRT_Error_Code_UNAVAILABLE == 14);
    CHECK(PJRT_Error_Code_DATA_LOSS == 15);
    CHECK(PJRT_Error_Code_UNAUTHENTICATED == 16);
    CHECK(PJRT_Extension_Type_Gpu_Custom_Call == 0);
    CHECK(PJRT_Extension_Type_Profiler == 1);
    CHECK(PJRT_Extension_Type_Custom_Partitioner == 2);
    CHECK(PJRT_Extension_Type_Stream == 3);
    CHECK(PJRT_Extension_Type_Layouts == 4);
    CHECK(PJRT_Extension_Type_FFI == 5);
    CHECK(PJRT_Extension_Type_MemoryDescriptions == 6);
    CHECK(PJRT_Extension_Type_XlaTransform == 24);
    CHECK(PJRT_Buffer_Type_INVALID == 0);
    CHECK(PJRT_Buffer_Type_PRED == 1);
    CHECK(PJRT_Buffer_Type_S8 == 2);

    /* 5. The export macro from the macros header is usable in a declaration (and is not empty-checked). */
    CHECK(exported_probe() == 42);

    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK pjrt-c-api PJRT_API %s\n", version);
    return 0;
}
