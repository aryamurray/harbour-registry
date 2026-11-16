/* Declarations of a few functions of libtorch's stable C ABI ("AOTInductor shim"), for C.
 *
 * Upstream declares them in <torch/csrc/inductor/aoti_torch/c/shim.h> and
 * <torch/csrc/inductor/aoti_torch/generated/c_shim_cpu.h>. Those headers are C++ only in 2.15.0
 * (`using AtenTensorHandle = ...;`, `c10::Half`, C++ includes), so a C program cannot include them
 * and has to repeat the prototypes. The functions themselves have C linkage and are exported. */
#ifndef TORCH_C_SHIM_H
#define TORCH_C_SHIM_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#else
#include <stdbool.h>
#endif

#if defined(_WIN32) && !defined(__GNUC__)
#define TORCH_C_IMPORT __declspec(dllimport)
#else
#define TORCH_C_IMPORT
#endif

struct AtenTensorOpaque;
typedef struct AtenTensorOpaque* TorchCTensor;   /* upstream: AtenTensorHandle */
typedef int32_t TorchCError;                     /* upstream: AOTITorchError; 0 is success */

TORCH_C_IMPORT uint64_t aoti_torch_abi_version(void);
TORCH_C_IMPORT int32_t aoti_torch_device_type_cpu(void);
TORCH_C_IMPORT int32_t aoti_torch_dtype_float32(void);
TORCH_C_IMPORT int32_t aoti_torch_dtype_int64(void);
TORCH_C_IMPORT size_t aoti_torch_dtype_element_size(int32_t dtype);
TORCH_C_IMPORT TorchCError aoti_torch_create_tensor_from_blob(
    void* data, int64_t ndim, const int64_t* sizes_ptr, const int64_t* strides_ptr, int64_t storage_offset,
    int32_t dtype, int32_t device_type, int32_t device_index, TorchCTensor* ret);
TORCH_C_IMPORT TorchCError aoti_torch_empty_strided(
    int64_t ndim, const int64_t* sizes_ptr, const int64_t* strides_ptr, int32_t dtype, int32_t device_type,
    int32_t device_index, TorchCTensor* ret_new_tensor);
TORCH_C_IMPORT TorchCError aoti_torch_delete_tensor_object(TorchCTensor tensor);
TORCH_C_IMPORT TorchCError aoti_torch_get_dim(TorchCTensor tensor, int64_t* ret_dim);
TORCH_C_IMPORT TorchCError aoti_torch_get_numel(TorchCTensor tensor, int64_t* ret_numel);
TORCH_C_IMPORT TorchCError aoti_torch_get_size(TorchCTensor tensor, int64_t d, int64_t* ret_size);
TORCH_C_IMPORT TorchCError aoti_torch_get_dtype(TorchCTensor tensor, int32_t* ret_dtype);
TORCH_C_IMPORT TorchCError aoti_torch_get_data_ptr(TorchCTensor tensor, void** ret_data_ptr);
TORCH_C_IMPORT TorchCError aoti_torch_item_float32(TorchCTensor tensor, float* ret_value);
TORCH_C_IMPORT TorchCError aoti_torch_scalar_to_tensor_float32(float value, TorchCTensor* ret_new_tensor);
TORCH_C_IMPORT TorchCError aoti_torch_clone(TorchCTensor self, TorchCTensor* ret);
TORCH_C_IMPORT TorchCError aoti_torch_zero_(TorchCTensor self);
TORCH_C_IMPORT TorchCError aoti_torch_cpu_add_Tensor(TorchCTensor self, TorchCTensor other, double alpha, TorchCTensor* ret0);
TORCH_C_IMPORT TorchCError aoti_torch_cpu_mul_Tensor(TorchCTensor self, TorchCTensor other, TorchCTensor* ret0);
TORCH_C_IMPORT TorchCError aoti_torch_cpu_mm_out(TorchCTensor out, TorchCTensor self, TorchCTensor mat2);

#ifdef __cplusplus
}
#endif
#endif
