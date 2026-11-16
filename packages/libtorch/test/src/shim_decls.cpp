// Compile-time proof that torch_c_shim.h (what the C file uses) agrees with upstream's own
// declarations: a C++ translation unit sees both, and a prototype that differs from upstream's
// is a conflicting declaration of a C-linkage function, which is an error.
#include <torch/csrc/inductor/aoti_torch/c/shim.h>
#include <torch/csrc/inductor/aoti_torch/generated/c_shim_cpu.h>
#include "torch_c_shim.h"
