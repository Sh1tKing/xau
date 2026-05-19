# AGENTS.md — XAUUSD AI Trading System

## Project overview

Single-file MT5 automated trading system for XAUUSD (Gold) on M5 timeframe. Uses a dual-AI-engine architecture backed by DeepSeek API: one AI acts as "Commander" for trade entry/exit decisions on M5 bars, another as "Risk Director" for stop-loss optimization on M1 bars. Python acts as the ultimate circuit-breaker — all AI outputs are validated by local rules before execution.

## Engineering Iron Rules (absolute)

### 1. No rewrites — surgical patching only

All modifications MUST be inserted into the existing `xauusd copy.py` as surgical additions. Creating an entirely new main program structure is forbidden. The existing class `TradingSystemWithAI`, its method layout, the `run_simulation` loop, and the `main()` entry point are the skeleton — inject into it, never replace it.

### 2. Bottom-layer API no-fly zone

The following methods and code blocks are **read-only** and must not be modified:

- `connect_mt5()` — MT5 initialization and login logic (lines 76–140)
- `get_market_data_and_features()` — historical data pulling and feature assembly (lines 284–458)
- `place_order()` — order sending function (lines 829–876)

### 3. AI & analysis no-fly zone

The following are **read-only** and must not be modified:

- H4 / H1 / M5 multi-timeframe trend extraction logic within `get_market_data_and_features()` (lines 376–395 for H4/H1, lines 298–340 for M5)
- `call_external_ai()` — Commander prompt structure and HTTP request framework (lines 460–590)
- `call_protection_ai()` — Risk Director prompt structure and HTTP request framework (lines 592–705)

### 4. Pure-function separation

All newly added risk-control logic or mathematical algorithms MUST be encapsulated in completely independent Python pure functions or classes. These must:

- Have zero network I/O
- Have zero file I/O
- Take data in, return data out
- Be callable from the main class's execution flow WITHOUT modifying the forbidden methods listed in Rules 2 and 3

## Architecture reference

```
Data layer:  get_market_data_and_features() → dict of all indicators + positions
     ↓
Hot loop (every 15s): run_simulation()
  1. Pull features (local, fast)
  2. Local trailing stop (update_trailing_stop) — pure math, no AI
  3. On new M1 bar → call_protection_ai() → execute_protection()
  4. On new M5 bar → call_external_ai() → execute_decision()
```

### Key design facts

- M5 MACD: fast=8, slow=17, signal=5
- M1 MACD: fast=5, slow=13, signal=4 (translated to 7 semantic labels before reaching AI)
- Max positions: 2 (hard cap in `execute_decision`)
- Lot size: fixed 0.01 (the `calculate_lot_size` body returns 0.01; the proportional logic is commented out)
- Trail stop: triggers when profit > max(3.0, ATR * 0.5), steps SL at price ± max(3.0, ATR * 0.8)
- ATR fail-safe: if AI returns invalid SL, Python enforces 1.5 × ATR
- Protection AI validates SL with 3-tier checks before dispatch
