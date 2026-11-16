#include <torch/torch.h>
#include <cstdio>
#include <cstring>

#define EXPECT(cond) do { if (!(cond)) { std::printf("FAIL %s (line %d)\n", #cond, __LINE__); return 1; } } while (0)

extern "C" int cxx_consumer(const char* want_version) {
    EXPECT(std::strcmp(TORCH_VERSION, want_version) == 0);
    torch::Tensor a = torch::tensor({1.0f, 2.0f, 3.0f, 4.0f}).reshape({2, 2});
    torch::Tensor b = torch::ones({2, 2}) * 2.0f;
    torch::Tensor c = a * b + 1.0f;                  // [[3,5],[7,9]]
    float sum = c.sum().item<float>();
    std::printf("sum = %g\n", sum);
    EXPECT(sum == 24.0f);
    EXPECT(torch::mm(a, a)[1][1].item<float>() == 22.0f);
    // autograd: d/dx sum(x^2) = 2x
    torch::Tensor x = torch::tensor({1.0f, 2.0f, 3.0f}, torch::requires_grad());
    (x * x).sum().backward();
    EXPECT(x.grad()[2].item<float>() == 6.0f);
    // an nn module and an optimiser step: the registrations inside torch_cpu are really there
    torch::manual_seed(0);
    torch::nn::Linear lin(3, 2);
    torch::optim::SGD sgd(lin->parameters(), 0.1);
    torch::Tensor loss = lin(torch::ones({4, 3})).pow(2).mean();
    float before = loss.item<float>();
    sgd.zero_grad();
    loss.backward();
    sgd.step();
    EXPECT(lin(torch::ones({4, 3})).pow(2).mean().item<float>() < before);
    std::printf("C++ API ok\n");
    return 0;
}
