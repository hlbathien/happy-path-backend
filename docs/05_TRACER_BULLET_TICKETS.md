# 05 — Spec-Driven & Property-Based Test-Driven Trajectory for Coding Agents

## 1. Why Single-Point "Happy-Hour" Tests Fail
When a ticket only tests a single departure time (`06:00`) and caps the agent at 1 simple assertion, an agent will satisfy that single data point while leaving 32 out of 48 hourly schedules broken, bypassing physical highway interchanges, or leaving failed `409` mutations persisted in SQLite.

## 2. The 4-Step Execution Protocol for Every Ticket (`Red -> Green -> Sweep -> Ablate`)
1. **Step 1 (Spec & Contract Lock)**: Read `AGENTS.md` and the ticket file in `.scratch/happy-path-autopilot/issues/<NN>-<slug>.md`. Trace all indirect dependencies across SQLite DDL, seed JSON, in-memory graph, solver, Pydantic schemas, and FastAPI routes.
2. **Step 2 (Write Property-Based & Adversarial Tests First)**: Implement the ticket's multi-hour sweep, boundary, rollback, and fault-injection assertions in `tests/test_corridors_e2e.py`.
3. **Step 3 (Root-Cause Implementation)**: Implement the domain/solver/API logic without fallback shortcuts or silent error swallowing.
4. **Step 4 (Ablation & Full-Matrix Verification)**: Run `make seed && make test && make openapi` and verify that ablating the ticket's core constraint immediately causes the ticket's test to fail.
