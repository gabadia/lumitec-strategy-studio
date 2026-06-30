# Studio Testing Strategy and Test Catalog

## Purpose

This document defines how to create and run a full testing pyramid for Lumitec Strategy Studio, with concrete test case names and exact assertions for future implementation.

It covers:
- Unit tests (backend pure logic)
- Integration tests (backend workflow and API contracts)
- Frontend tests (component and app-flow behavior)
- End-to-end tests (real user journeys)
- CI sequencing and rollout plan

---

## Test Objectives

Primary objectives:
1. Prevent regressions in strategy lifecycle validation.
2. Guarantee explicit, user-facing error messages for quote/bar symmetry issues.
3. Ensure validation, submit, and resubmit gates block invalid strategies.
4. Verify bars-only strategies do not require quote subscriptions.
5. Confirm Generate -> Validate -> Submit UI workflow behavior remains consistent.

---

## Scope Under Test

Backend:
- backend/agent.py
- backend/main.py

Frontend:
- frontend/src/App.tsx
- frontend/src/components/ActivityFeed.tsx
- frontend/src/components/StatusStepper.tsx
- frontend/src/components/IntentInput.tsx
- frontend/src/types.ts

External dependencies to mock/stub in most tests:
- Validator service (/validate)
- Orchestrator submit endpoint
- SSE event streams
- LLM providers

---

## Recommended Test Stack

Backend:
- pytest
- pytest-asyncio
- httpx mocking (respx or pytest-httpx)

Frontend:
- vitest
- @testing-library/react
- @testing-library/user-event

E2E:
- Playwright

---

## Proposed Test Layout

- backend/tests/unit
- backend/tests/integration
- backend/tests/contracts
- frontend/src/__tests__
- frontend/e2e

---

## Unit Test Plan (Backend)

### File: backend/tests/unit/test_market_data_lifecycle_checker.py

#### Test Case 1
- Name: test_lifecycle_checker_bars_only_valid_returns_no_errors
- Setup: strategy code has subscribe_market_data_bars in on_start and unsubscribe_market_data_bars in on_stop.
- Assertion:
  - _check_market_data_lifecycle(code) == []

#### Test Case 2
- Name: test_lifecycle_checker_bars_only_missing_bar_unsubscribe_returns_expected_error
- Setup: strategy code has subscribe_market_data_bars in on_start and no bar unsubscribe in on_stop.
- Assertion:
  - _check_market_data_lifecycle(code) == ["Bar subscription found but matching bar unsubscription missing in on_stop"]

#### Test Case 3
- Name: test_lifecycle_checker_quotes_only_valid_returns_no_errors
- Setup: strategy code has subscribe_market_data(... subscribe_quotes=True ...) in on_start and matching unsubscribe_market_data in on_stop.
- Assertion:
  - _check_market_data_lifecycle(code) == []

#### Test Case 4
- Name: test_lifecycle_checker_quotes_only_missing_quote_unsubscribe_returns_expected_error
- Setup: strategy code has quote subscribe in on_start and no quote unsubscribe in on_stop.
- Assertion:
  - _check_market_data_lifecycle(code) == ["Quote subscription found but matching quote unsubscription missing in on_stop"]

#### Test Case 5
- Name: test_lifecycle_checker_mixed_subscriptions_missing_quote_unsubscribe_returns_only_quote_error
- Setup: strategy code has both bar and quote subscribe in on_start; on_stop has only bar unsubscribe.
- Assertion:
  - _check_market_data_lifecycle(code) == ["Quote subscription found but matching quote unsubscription missing in on_stop"]

#### Test Case 6
- Name: test_lifecycle_checker_subscription_present_and_on_stop_missing_returns_expected_error
- Setup: strategy code has any market-data subscription and no on_stop method.
- Assertion:
  - _check_market_data_lifecycle(code) == ["on_stop is missing while market-data subscriptions are present"]

#### Test Case 7
- Name: test_lifecycle_checker_no_market_data_subscriptions_returns_no_errors
- Setup: strategy code has neither bar nor quote subscriptions.
- Assertion:
  - _check_market_data_lifecycle(code) == []

#### Test Case 8
- Name: test_lifecycle_checker_invalid_python_returns_no_lifecycle_errors
- Setup: malformed strategy code (SyntaxError).
- Assertion:
  - _check_market_data_lifecycle(code) == []

### File: backend/tests/unit/test_strategy_method_source.py

#### Test Case 9
- Name: test_get_strategy_method_source_returns_on_stop_body_for_lumitec_strategy
- Setup: code contains class Foo(LumitecBaseStrategy) with on_stop.
- Assertion:
  - returned value is not None
  - returned value contains "def on_stop"

#### Test Case 10
- Name: test_get_strategy_method_source_returns_none_when_strategy_class_not_found
- Setup: code contains no class inheriting LumitecBaseStrategy.
- Assertion:
  - _get_strategy_method_source(code, "on_stop") is None

#### Test Case 11
- Name: test_get_strategy_method_source_returns_none_when_method_missing
- Setup: strategy class exists but method does not.
- Assertion:
  - _get_strategy_method_source(code, "on_stop") is None

---

## Integration Test Plan (Backend Workflow)

### File: backend/tests/integration/test_validation_gate_lifecycle.py

#### Test Case 12
- Name: test_phase_openai_validate_short_circuits_on_local_lifecycle_error_without_calling_validator
- Setup:
  - Input code violates lifecycle policy (bar subscribe, missing bar unsubscribe).
  - Mock validator endpoint; track call count.
- Assertions:
  - emitted tool_result has failed=True
  - emitted content contains "Bar subscription found but matching bar unsubscription missing in on_stop"
  - validator endpoint call count == 0

#### Test Case 13
- Name: test_phase_openai_validate_calls_external_validator_when_local_lifecycle_passes
- Setup:
  - Input code passes lifecycle policy.
  - Mock /validate returns validated=True.
- Assertions:
  - validator endpoint call count == 1
  - emitted _validation_done has passed=True

### File: backend/tests/integration/test_submit_gate_lifecycle.py

#### Test Case 14
- Name: test_phase_submit_blocks_on_local_lifecycle_error_before_external_validator
- Setup:
  - Input code violates quote teardown.
  - Mock validator and orchestrator endpoints; track calls.
- Assertions:
  - emitted _submit_done has success=False
  - emitted text includes "Cannot submit — validation failed"
  - emitted content contains "Quote subscription found but matching quote unsubscription missing in on_stop"
  - validator endpoint call count == 0
  - orchestrator submit call count == 0

#### Test Case 15
- Name: test_phase_submit_calls_external_validator_and_orchestrator_when_local_lifecycle_passes
- Setup:
  - Lifecycle-valid code.
  - Mock validator returns validated=True.
  - Mock orchestrator returns success status payload.
- Assertions:
  - emitted _submit_done has success=True
  - validator call count == 1
  - orchestrator submit call count == 1

### File: backend/tests/integration/test_resubmit_gate_lifecycle.py

#### Test Case 16
- Name: test_run_resubmit_workflow_blocks_on_local_lifecycle_error_before_external_validator
- Setup:
  - Lifecycle-invalid code.
  - Mock validator/orchestrator endpoints; track calls.
- Assertions:
  - emitted message contains "Resubmit blocked — validation failed"
  - emitted content contains the exact lifecycle error string
  - validator endpoint call count == 0
  - orchestrator submit call count == 0

#### Test Case 17
- Name: test_run_resubmit_workflow_proceeds_when_local_lifecycle_passes
- Setup:
  - Lifecycle-valid code.
  - Mock validator returns validated=True.
  - Mock orchestrator returns success.
- Assertions:
  - emits strategy_submitted event
  - emits done event
  - validator endpoint call count >= 1
  - orchestrator submit call count == 1

### File: backend/tests/integration/test_run_strategy_workflow_lifecycle.py

#### Test Case 18
- Name: test_run_strategy_workflow_returns_validation_failed_summary_when_lifecycle_violation_persists
- Setup:
  - Generation phase returns lifecycle-invalid code.
  - Fix model does not repair within max attempts.
- Assertions:
  - emitted text contains "Validation failed after maximum attempts. Cannot submit."
  - emits done event

#### Test Case 19
- Name: test_run_strategy_workflow_bars_only_valid_reaches_params_ready
- Setup:
  - Generation phase returns lifecycle-valid bars-only code.
  - External validator returns validated=True.
- Assertions:
  - emits params_ready event
  - params_ready contains parsed legs/params
  - emits done event

---

## API Integration Tests (Backend HTTP)

### File: backend/tests/integration/test_main_api_run_and_qa.py

#### Test Case 20
- Name: test_run_endpoint_surfaces_lifecycle_failure_in_stream
- Setup: invoke /run endpoint with lifecycle-invalid strategy flow (mock internals as needed).
- Assertions:
  - SSE stream includes failed validation tool_result
  - SSE stream includes exact lifecycle error text

#### Test Case 21
- Name: test_run_endpoint_bars_only_valid_flow_reaches_params_ready
- Setup: invoke /run endpoint with bars-only valid strategy flow.
- Assertions:
  - SSE stream includes params_ready
  - SSE stream ends with done

---

## Frontend Test Plan

### File: frontend/src/__tests__/app.lifecycle-errors.test.tsx

#### Test Case 22
- Name: renders_bar_lifecycle_error_message_in_activity_feed
- Setup:
  - Mock /api/run-strategy SSE to emit failed tool_result with bar lifecycle error.
- Assertions:
  - UI shows "Bar subscription found but matching bar unsubscription missing in on_stop"
  - stepper highlights validating stage as failed/active behavior per design

#### Test Case 23
- Name: renders_quote_lifecycle_error_message_in_activity_feed
- Setup:
  - Mock SSE with quote lifecycle failure.
- Assertions:
  - UI shows "Quote subscription found but matching quote unsubscription missing in on_stop"

#### Test Case 24
- Name: bars_only_valid_flow_does_not_show_quote_error
- Setup:
  - Mock bars-only valid flow through params_ready.
- Assertions:
  - quote error text is absent
  - params review UI is shown

### File: frontend/src/__tests__/status-stepper.lifecycle.test.tsx

#### Test Case 25
- Name: status_stepper_transitions_to_validating_on_validate_tool_events
- Setup: feed validate_strategy tool events into app state.
- Assertions:
  - step value transitions to validating

#### Test Case 26
- Name: status_stepper_stops_before_submitting_when_validation_fails
- Setup: failed validation flow.
- Assertions:
  - submitting step is not entered

---

## End-to-End Test Plan (Playwright)

### File: frontend/e2e/lifecycle-validation.spec.ts

#### Test Case 27
- Name: e2e_blocks_submit_for_bars_missing_bar_teardown
- User flow:
  - Open Studio
  - Submit intent/code that contains bar subscribe and missing bar unsubscribe
- Assertions:
  - validation error shown with exact message:
    - "Bar subscription found but matching bar unsubscription missing in on_stop"
  - no strategy_submitted success indication appears

#### Test Case 28
- Name: e2e_blocks_submit_for_quotes_missing_quote_teardown
- User flow:
  - Submit intent/code with quote subscribe and missing quote unsubscribe
- Assertions:
  - validation error shown with exact message:
    - "Quote subscription found but matching quote unsubscription missing in on_stop"
  - no successful submit indicator

#### Test Case 29
- Name: e2e_allows_bars_only_strategy_without_quotes
- User flow:
  - Submit bars-only valid strategy
- Assertions:
  - no quote lifecycle error appears
  - flow reaches params review or submit success path (depending on fixture)

#### Test Case 30
- Name: e2e_missing_on_stop_with_subscriptions_is_rejected
- User flow:
  - Submit strategy with market-data subscription and no on_stop
- Assertions:
  - UI shows exact error:
    - "on_stop is missing while market-data subscriptions are present"

---

## Test Data Fixtures

Suggested fixture files (future):
- backend/tests/fixtures/strategies/bars_only_valid.py
- backend/tests/fixtures/strategies/bars_only_missing_unsub.py
- backend/tests/fixtures/strategies/quotes_only_valid.py
- backend/tests/fixtures/strategies/quotes_only_missing_unsub.py
- backend/tests/fixtures/strategies/missing_on_stop_with_subscriptions.py
- backend/tests/fixtures/strategies/no_subscriptions.py

Fixture rules:
- Keep each fixture minimal.
- One behavior per fixture.
- Include clear docstring with expected pass/fail reason.

---

## Command Plan (Future)

Backend unit:
```bash
cd backend
source .venv/bin/activate
pytest backend/tests/unit -q
```

Backend integration:
```bash
cd backend
source .venv/bin/activate
pytest backend/tests/integration -q
```

Frontend tests:
```bash
cd frontend
npm test
```

E2E:
```bash
cd frontend
npx playwright test
```

All tests (example CI order):
1. backend unit
2. backend integration
3. frontend unit/integration
4. e2e

---

## CI and Quality Gates

Suggested merge gates:
1. Required pass: backend unit and backend integration.
2. Required pass: frontend unit/integration.
3. Required pass on protected branches: E2E lifecycle suite.
4. Required assertion: lifecycle error strings remain exact and unchanged.

---

## Rollout Sequence

Phase 1:
- Implement unit tests 1-11.

Phase 2:
- Implement backend integration tests 12-21.

Phase 3:
- Implement frontend tests 22-26.

Phase 4:
- Implement Playwright E2E tests 27-30.

Phase 5:
- Wire all suites to CI with required checks.

---

## Non-Goals

Not in scope for this test catalog:
- Performance/load testing
- Security penetration testing
- Orchestrator internal behavior validation
- External validator service correctness beyond contract stubs

---

## Maintenance Notes

1. Keep this catalog aligned with lifecycle policy text and exact error messages.
2. Treat exact lifecycle error strings as contract-level assertions.
3. When changing policy wording, update both tests and prompts in the same change.
4. Add regression tests for every production defect in lifecycle validation.
