"""
Copyright (c) 2023 by FlashInfer team.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import numpy
import pytest
import torch

import flashinfer


def ceil_div(a, b):
    return (a + b - 1) // b


@pytest.mark.parametrize("stage", ["decode", "append"])
@pytest.mark.parametrize("batch_size", [12, 17])
@pytest.mark.parametrize("unique_kv_len", [37, 17])
@pytest.mark.parametrize("shared_kv_len", [128, 512, 2048])
@pytest.mark.parametrize("num_heads", [8, 16])
@pytest.mark.parametrize("causal", [False, True])
@pytest.mark.parametrize("head_dim", [128, 256])
@pytest.mark.parametrize("page_size", [1, 16])
def test_batch_attention_with_shared_prefix_paged_kv_cache(
    stage,
    batch_size,
    unique_kv_len,
    shared_kv_len,
    num_heads,
    causal,
    head_dim,
    page_size,
):
    if stage == "decode" and causal == True:
        pytest.skip("Causal attention is not required in decode stage")
    assert shared_kv_len % page_size == 0
    kv_layout = "NHD"
    if stage == "append":
        q = torch.randn(batch_size * unique_kv_len, num_heads, head_dim).to(0).half()
        q_indptr = torch.arange(0, batch_size + 1).to(0).int() * unique_kv_len
    else:
        q = torch.randn(batch_size, num_heads, head_dim).to(0).half()
        q_indptr = torch.arange(0, batch_size + 1).to(0).int()
    k_shared = torch.randn(shared_kv_len, num_heads, head_dim).to(0).half()
    v_shared = torch.randn(shared_kv_len, num_heads, head_dim).to(0).half()
    k_unique = torch.randn(batch_size * unique_kv_len, num_heads, head_dim).to(0).half()
    v_unique = torch.randn(batch_size * unique_kv_len, num_heads, head_dim).to(0).half()

    kv_data = (
        torch.zeros(
            ceil_div(shared_kv_len, page_size)
            + batch_size * ceil_div(unique_kv_len, page_size),
            2,
            page_size,
            num_heads,
            head_dim,
        )
        .to(0)
        .half()
    )
    shared_kv_indices = torch.arange(0, ceil_div(shared_kv_len, page_size)).to(0).int()
    shared_append_indptr = torch.arange(0, 2).to(0).int() * shared_kv_len
    shared_kv_indptr = torch.arange(0, 2).to(0).int() * ceil_div(
        shared_kv_len, page_size
    )
    shared_last_page_len = torch.full(
        (1,), (shared_kv_len - 1) % page_size + 1, dtype=torch.int32
    ).to(0)
    flashinfer.append_paged_kv_cache(
        k_shared,
        v_shared,
        shared_append_indptr,
        kv_data,
        shared_kv_indices,
        shared_kv_indptr,
        shared_last_page_len,
        kv_layout,
    )
    unique_kv_indices = torch.arange(
        0, batch_size * ceil_div(unique_kv_len, page_size)
    ).to(0).int() + ceil_div(shared_kv_len, page_size)
    unique_append_indptr = torch.arange(0, batch_size + 1).to(0).int() * unique_kv_len
    unique_kv_indptr = torch.arange(0, batch_size + 1).to(0).int() * ceil_div(
        unique_kv_len, page_size
    )
    unique_last_page_len = torch.full(
        (batch_size,), (unique_kv_len - 1) % page_size + 1, dtype=torch.int32
    ).to(0)
    flashinfer.append_paged_kv_cache(
        k_unique,
        v_unique,
        unique_append_indptr,
        kv_data,
        unique_kv_indices,
        unique_kv_indptr,
        unique_last_page_len,
        kv_layout,
    )

    if stage == "decode":
        baseline_wrapper = flashinfer.BatchDecodeWithPagedKVCacheWrapper(
            torch.empty(32 * 1024 * 1024, dtype=torch.int8).to(0), kv_layout
        )
        cascade_wrapper = flashinfer.BatchDecodeWithSharedPrefixPagedKVCacheWrapper(
            torch.empty(32 * 1024 * 1024, dtype=torch.int8).to(0), kv_layout
        )
    else:
        baseline_wrapper = flashinfer.BatchPrefillWithPagedKVCacheWrapper(
            torch.empty(32 * 1024 * 1024, dtype=torch.int8).to(0), kv_layout
        )
        cascade_wrapper = flashinfer.BatchPrefillWithSharedPrefixPagedKVCacheWrapper(
            torch.empty(32 * 1024 * 1024, dtype=torch.int8).to(0), kv_layout
        )

    baseline_kv_indices_arr = []
    for i in range(batch_size):
        baseline_kv_indices_arr.append(
            torch.arange(0, ceil_div(shared_kv_len, page_size)).int()
        )
        baseline_kv_indices_arr.append(
            torch.arange(
                i * ceil_div(unique_kv_len, page_size),
                (i + 1) * ceil_div(unique_kv_len, page_size),
            ).int()
            + ceil_div(shared_kv_len, page_size)
        )
    baseline_kv_indices = torch.cat(baseline_kv_indices_arr, dim=0).to(0)
    baseline_kv_indptr = torch.arange(0, batch_size + 1).to(0).int() * (
        ceil_div(shared_kv_len, page_size) + ceil_div(unique_kv_len, page_size)
    )
    baseline_kv_last_page_len = unique_last_page_len
    if stage == "decode":
        baseline_wrapper.begin_forward(
            baseline_kv_indptr,
            baseline_kv_indices,
            baseline_kv_last_page_len,
            num_heads,
            num_heads,
            head_dim,
            page_size,
        )
        o_baseline = baseline_wrapper.forward(q, kv_data)
    else:
        baseline_wrapper.begin_forward(
            q_indptr,
            baseline_kv_indptr,
            baseline_kv_indices,
            baseline_kv_last_page_len,
            num_heads,
            num_heads,
            head_dim,
            page_size,
        )
        o_baseline = baseline_wrapper.forward(q, kv_data, causal=causal)

    cascade_kv_indices = unique_kv_indices
    cascade_kv_indptr = unique_kv_indptr
    cascade_kv_last_page_len = unique_last_page_len

    if stage == "decode":
        cascade_wrapper.begin_forward(
            cascade_kv_indptr,
            cascade_kv_indices,
            cascade_kv_last_page_len,
            num_heads,
            num_heads,
            head_dim,
            page_size,
        )
        o_cascade = cascade_wrapper.forward(q, k_shared, v_shared, kv_data)
    else:
        cascade_wrapper.begin_forward(
            q_indptr,
            cascade_kv_indptr,
            cascade_kv_indices,
            cascade_kv_last_page_len,
            num_heads,
            num_heads,
            head_dim,
            page_size,
        )
        o_cascade = cascade_wrapper.forward(
            q, k_shared, v_shared, kv_data, causal=causal
        )

    numpy.testing.assert_allclose(
        o_baseline.cpu().numpy(), o_cascade.cpu().numpy(), rtol=1e-3, atol=1e-3
    )


if __name__ == "__main__":
    test_batch_attention_with_shared_prefix_paged_kv_cache(
        "decode", 12, 37, 128, 8, False, 128, 16
    )
    test_batch_attention_with_shared_prefix_paged_kv_cache(
        "apppend", 12, 37, 128, 8, True, 128, 16
    )
