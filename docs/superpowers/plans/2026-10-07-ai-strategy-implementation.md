# AI Strategy Evidence Implementation Plan

> **Status:** Implementation complete; local self-tests and static checks pass. Real-model replay and profitability comparison remain future validation work.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved three-AI strategy design while preserving local coarse filtering, AI-1 autonomous proposals, AI-2 review, AI-3 guardianship, and all existing hard risk limits.

**Architecture:** Keep `xauusd_fib_volume_pa_trader.py` responsible for deterministic execution and guardian state. Keep `xauusd_fib_volume_pa_ai3.py` responsible for evidence packets, prompt roles, proposal provenance, and recoverable AI-3 context. Add only evidence/state plumbing and final execution checks; do not rewrite local scoring or setup rules.

**Tech Stack:** Python 3, pandas, MetaTrader5 Python API, existing self-test entry points, JSON state under the ignored log directory.

## Global Constraints

- Preserve H1 direction, M15 structure, M5 timing, `pullback`/`range_edge`/`breakout` setup rules, and autonomous AI-1 proposals.
- Preserve local threshold 55, AI threshold 75, minimum RR 1.5, maximum SL distance 8 USD, 0.01 lot, and one-position limit.
- Python remains the final gate; AI cannot widen stops or override hard limits.
- Do not replace the M15 reference entry price with the newest M5 close.
- Do not start or restart MT5, live trading, or demo trading processes.
- Do not stage `.env`, logs, `xau_fib_pa_logs/`, backups, or unrelated untracked files.

---

### Task 1: Correct deterministic execution and guardian evidence

**Files:**
- Modify: `xauusd_fib_volume_pa_trader.py:1800-2050`
- Test: `xauusd_fib_volume_pa_trader.py` self-test and focused inline assertions

**Interfaces:**
- `FibVolumePATrader.place_order(result)` keeps its boolean return value and adds a final live-risk rejection before `order_send`.
- `GuardianState.highest_price` and `GuardianState.lowest_price` remain the MFE/MAE extrema and are updated for both directions on every guardian pass.

- [x] **Step 1: Add a failing focused assertion for live risk.** A deterministic directional stop-distance assertion covers both BUY and SELL paths and the same helper used by the final order gate.
- [x] **Step 2: Implement the live final gate.** `place_order` validates directional live risk against `MAX_SL_ABS` before order submission, then retains the live RR check without changing the reference price.
- [x] **Step 3: Fix both-sided extrema.** Each BUY and SELL guardian pass updates both `highest_price` and `lowest_price` before calculating favorable and adverse movement.
- [x] **Step 4: Run verification.** The trader compile and self-test pass without starting an order process.

### Task 2: Make the AI evidence packet and final proposal auditable

**Files:**
- Modify: `xauusd_fib_volume_pa_ai3.py:70-180, 680-790`
- Modify: `xauusd_fib_volume_pa_trader.py:1390-1555`
- Test: `xauusd_fib_volume_pa_ai3.py --self-test` and packet construction assertions

**Interfaces:**
- `build_feature_pack(df_m15, df_h1, df_m5, local_result)` continues returning `(pack, fib, atr, price)` and adds `m15_bars` sufficient for the existing 24-bar range and 16-bar breakout evidence, plus explicit reference-price and closed-bar metadata.
- `StrategyResult.details` continues carrying local details and accepts proposal provenance values needed by AI-2 and AI-3.

- [x] **Step 1: Add deterministic packet assertions.** The AI3 self-test checks closed-bar metadata, reference price, setup, local action, 24-bar M15 evidence, and 3-bar M5 evidence.
- [x] **Step 2: Expand evidence without changing scoring.** The packet sends the existing calculated windows and exact bar timestamps; no second Fib/range algorithm or score change was added.
- [x] **Step 3: Record proposal provenance.** Parsed AI-1 stop/target, local reference entry, final Python stop/target, fallback/clamp reason, `decision_id`, setup, session, source, and evidence fields reach AI-2 and logs.
- [x] **Step 4: Review the final plan.** AI-2 receives the Python-adjusted final proposal and reason; clamp, hard-rejection, and packet assertions pass in the AI3 self-test.

### Task 3: Align prompts and persist AI-3 entry context

**Files:**
- Modify: `xauusd_fib_volume_pa_ai3.py:75-115, 520-560, 790-900`
- Modify: `xauusd_fib_volume_pa_trader.py:1550-1570, 1900-1940`
- Create at runtime only: `xau_fib_pa_logs/ai3_position_state.json` (ignored, never commit)
- Test: AI3 self-test, JSON round-trip assertions, and prompt packet assertions

**Interfaces:**
- `ThreeAITrader` owns a JSON-backed map keyed by MT5 position ticket/identifier. Each record contains final entry, initial SL/TP/risk, setup/session, decision id, trigger/invalidating boundary, target evidence, and source (`local` or `ai1`).
- State load failure returns an explicit unknown context; it never fills missing MFE/MAE or history with fabricated zero values.

- [x] **Step 1: Add state round-trip assertions.** The AI3 self-test serializes and reloads numeric risk/evidence state; active positions without recoverable state are marked unknown after restart.
- [x] **Step 2: Save final entry context after a successful order.** Actual ticket, fill, initial SL/TP/risk, and provenance are persisted atomically under the ignored log directory.
- [x] **Step 3: Load and reconcile state on startup.** Startup retains active matching contexts, marks missing history unknown, and keeps original risk independent of later SL tightening.
- [x] **Step 4: Send context to AI-3.** Saved reason, source, setup, invalidation, target basis, initial risk, coverage, and observed MFE/MAE are sent; unavailable values remain null/unknown.
- [x] **Step 5: Replace unsupported AI-3 claims.** AI-3 now requires a named closed-bar invalidation or consecutive adverse M1 plus M5 deterioration before early close.

### Task 4: Deterministic scenario verification and delivery

**Files:**
- Modify: `xauusd_fib_volume_pa_ai3.py` self-test section
- Modify: `xauusd_fib_volume_pa_trader.py` self-test section if needed
- Update: `AGENTS.md` and `md/ai_strategy_review_20261007.md` with verified behavior only

- [x] **Step 1: Add scenario cases.** Existing deterministic self-tests cover structure invalidation, H1 alignment, stop/RR obstacles, ordinary-volume and setup selection, breakout/retest, tight-range behavior, and BUY/SELL MFE/MAE symmetry; AI-3 single-bar versus consecutive-close semantics are enforced in the prompt and remain model-replay work.
- [x] **Step 2: Run all checks.** Compile, both self-tests, and `git diff --check` pass without starting `run()` or an MT5 process.
- [x] **Step 3: Review the diff against the design.** Local score, setup rules, thresholds, stop cap, RR floor, reference-price input, and AI autonomy remain unchanged.
- [x] **Step 4: Save memory and commit.** Agentmemory was updated with the verified behavior; the implementation is ready for the local `master` commit.

## Self-Review

- The plan covers evidence, hard execution checks, autonomous AI-1 proposals, independent AI-2 review, AI-3 context, state recovery, and scenario verification.
- No task changes local scoring or replaces the M15 reference entry price.
- Runtime state is explicitly ignored and never staged.
- The plan does not claim profitability; later validation must use frozen inputs and an untouched holdout window.
