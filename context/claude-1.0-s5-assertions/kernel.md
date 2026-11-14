# L1 단정 이관 원장 (진행 중)

미이관 항목은 원본을 보존하며 완료 전에 개별 대조한다.

- `test/cooldown-guards.test.mjs::cooldown 계약: 거대·비정상 만료값은 식힘으로 보지 않는다 (영구 활성 방지)` → 미대조
- `test/cooldown-guards.test.mjs::cooldown 계약: 미래 mtime은 창을 영구화하지 않고, 관용 범위 안의 시계 오차는 존중한다` → 미대조
- `test/cooldown-guards.test.mjs::compile cooldown: 오염된 만료값(1e300)이 compile을 영구 정지시키지 못한다` → 미대조
- `test/cooldown-guards.test.mjs::dedup judge cooldown: 오염된 만료값이 판정을 영구 정지시키지 못한다` → 미대조
- `test/cooldown-guards.test.mjs::시간 게이트 판정은 cooldown.py 한 벌을 거친다 (판정이 두 벌로 갈리는 재발 방지)` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_fenced_host_uses_warm_owned_provider_and_cold_lexical` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_real_provider_with_actual_selected_snapshot_proof` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_provider_child_crash_is_not_treated_as_unavailable` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_provider_invalid_prompt_is_not_treated_as_unavailable` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_provider_trickled_socket_does_not_extend_absolute_deadline` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_approved_vector_uses_real_provider_socket_once` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_verified_v6_snapshot_can_query_same_profile_candidate_hybrid` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_candidate_profile_requires_same_approved_provider_recipe` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_descriptor_binds_sealed_vector_profile` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_descriptor_rejects_changed_git_index_on_same_branch` → 미대조
- `test/fixtures/laya-reader-capabilities-checks.py::test_candidate_descriptor_reads_bound_checkout_without_writes` → 미대조
- `test/hook-budget.test.mjs::hook budget accepts 2-20 seconds and falls back to 18` → 미대조
- `test/hook-budget.test.mjs::an exhausted hook deadline still exits zero without stderr` → 미대조
- `test/fixtures/laya-installed-bytecode-checks.py::test_host_then_direct_setup_keeps_installed_tree_exact` → 미대조
- `test/fixtures/laya-installed-bytecode-checks.py::test_planted_bytecode_still_fails_exact_attestation` → 미대조
