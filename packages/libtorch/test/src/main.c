/* A C program over libtorch: compiled as C11 by the C compiler, `main` is here.
 *
 * It calls libtorch's C ABI (the "AOTInductor shim", exported from torch_cpu with C linkage):
 * tensors over memory this program owns, shape/dtype queries, ATen operators, an error returned as
 * a code, deletion. Upstream's header for these functions is C++-only in this release, so the
 * prototypes are repeated in torch_c_shim.h, and shim_decls.cpp proves at compile time that they
 * match upstream's. <torch/headeronly/version.h> is valid C and is included directly.
 *
 * Then it calls the C++ consumer (consumer.cpp), and prints OK last. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <torch/headeronly/version.h>   /* valid C: TORCH_VERSION, TORCH_ABI_VERSION */
#include "torch_c_shim.h"

#define EXPECT(cond) do { if (!(cond)) { printf("FAIL %s (line %d)\n", #cond, __LINE__); return 1; } } while (0)

int cxx_consumer(const char* want_version);

static int c_shim(void) {
    EXPECT(aoti_torch_abi_version() == TORCH_ABI_VERSION);
    int32_t f32 = aoti_torch_dtype_float32(), cpu = aoti_torch_device_type_cpu();
    EXPECT(aoti_torch_dtype_element_size(f32) == sizeof(float));

    float a_data[4] = {1, 2, 3, 4}, b_data[4] = {10, 20, 30, 40};
    int64_t sizes[2] = {2, 2}, strides[2] = {2, 1};
    TorchCTensor a = NULL, b = NULL, sum = NULL, prod = NULL, mm = NULL;
    EXPECT(aoti_torch_create_tensor_from_blob(a_data, 2, sizes, strides, 0, f32, cpu, 0, &a) == 0);
    EXPECT(aoti_torch_create_tensor_from_blob(b_data, 2, sizes, strides, 0, f32, cpu, 0, &b) == 0);

    int64_t dim = 0, numel = 0, size1 = 0; int32_t dtype = -1; void* ptr = NULL;
    EXPECT(aoti_torch_get_dim(a, &dim) == 0 && dim == 2);
    EXPECT(aoti_torch_get_numel(a, &numel) == 0 && numel == 4);
    EXPECT(aoti_torch_get_size(a, 1, &size1) == 0 && size1 == 2);
    EXPECT(aoti_torch_get_dtype(a, &dtype) == 0 && dtype == f32);
    EXPECT(aoti_torch_get_data_ptr(a, &ptr) == 0 && ptr == (void*)a_data);   /* from_blob borrows, no copy */

    /* a + 2*b = [[21,42],[63,84]] */
    EXPECT(aoti_torch_cpu_add_Tensor(a, b, 2.0, &sum) == 0);
    EXPECT(aoti_torch_get_data_ptr(sum, &ptr) == 0);
    EXPECT(((float*)ptr)[0] == 21.0f && ((float*)ptr)[3] == 84.0f);
    /* a * b elementwise = [[10,40],[90,160]] */
    EXPECT(aoti_torch_cpu_mul_Tensor(a, b, &prod) == 0);
    EXPECT(aoti_torch_get_data_ptr(prod, &ptr) == 0);
    EXPECT(((float*)ptr)[1] == 40.0f && ((float*)ptr)[2] == 90.0f);
    /* matrix product a @ a = [[7,10],[15,22]], written into a tensor torch allocates */
    EXPECT(aoti_torch_empty_strided(2, sizes, strides, f32, cpu, 0, &mm) == 0);
    EXPECT(aoti_torch_cpu_mm_out(mm, a, a) == 0);
    EXPECT(aoti_torch_get_data_ptr(mm, &ptr) == 0);
    EXPECT(((float*)ptr)[0] == 7.0f && ((float*)ptr)[3] == 22.0f);

    /* an error is a return code, not a crash: shapes that cannot be multiplied */
    int64_t sizes3[2] = {1, 4}, strides3[2] = {4, 1};
    TorchCTensor row = NULL, scalar = NULL; float item = 0;
    EXPECT(aoti_torch_create_tensor_from_blob(a_data, 2, sizes3, strides3, 0, f32, cpu, 0, &row) == 0);
    EXPECT(aoti_torch_cpu_mm_out(mm, row, row) != 0);
    EXPECT(aoti_torch_scalar_to_tensor_float32(2.5f, &scalar) == 0);
    EXPECT(aoti_torch_item_float32(scalar, &item) == 0 && item == 2.5f);

    EXPECT(aoti_torch_delete_tensor_object(a) == 0);
    EXPECT(aoti_torch_delete_tensor_object(b) == 0);
    EXPECT(aoti_torch_delete_tensor_object(sum) == 0);
    EXPECT(aoti_torch_delete_tensor_object(prod) == 0);
    EXPECT(aoti_torch_delete_tensor_object(mm) == 0);
    EXPECT(aoti_torch_delete_tensor_object(row) == 0);
    EXPECT(aoti_torch_delete_tensor_object(scalar) == 0);

    return 0;
}

int main(void) {
    const char* want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (!want) { printf("FAIL REGISTRY_TEST_UPSTREAM_VERSION is not set\n"); return 1; }
    if (strcmp(want, TORCH_VERSION) != 0) { printf("FAIL version %s != %s\n", TORCH_VERSION, want); return 1; }
    if (c_shim() != 0) return 1;
    printf("C ABI ok: tensors from memory, queries, add/mul/mm, error code, delete\n");
    if (cxx_consumer(want) != 0) return 1;
    printf("OK libtorch %s (C++ API and C stable-ABI shim)\n", TORCH_VERSION);
    return 0;
}
