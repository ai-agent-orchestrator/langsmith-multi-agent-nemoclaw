"""Run the ten unmodified upstream tests from the vendored snapshot."""

from third_party.multi_agent_module_lib.tests.test_examples import (
    test_guard_wrapper,
    test_guardrail_blocks_and_allows,
    test_guardrail_verifier_drops_false_positive,
    test_langgraph_matches_plain,
    test_loop_stops_on_done_and_caps,
    test_pipeline_approves_after_retry,
    test_pipeline_round_cap,
    test_prompt_loader,
    test_review_aggregation,
    test_run_reviewers_parallel,
)
