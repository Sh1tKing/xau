# Independent crypto DEMO fork

Source copied from gold runtime7f3df37. Clean public parent00376b31e5b1c0d6b6ac02a408174a276df30898; never push root master history.

Only run.py owns terminal operations and guardian loops. manual_order.py is a one-shot local command client, with no MT5/AI/Feishu transport. Do not import root trader/strategies, read root .env, modify gold files/state, or start terminal/trading/paid AI tests. User configures and starts after review.

Gold SL8 is a gold price distance. Here8USD is bounded account cash loss including configured costs; price distance uses asset ATR/percentage cap. Lots use broker cash calculations and floor to step, never fixed.01. Do not shorten structure/M5 coverage to fit cash/cap. H1, RR1.5, EMA1.5ATR remain hard gates.

USD DEMO only. Observe/dry-run prohibit all order mutations. Entry inspects all account positions; guardian owns exact symbol/magic. Identity switches and unresolved account receipts block entries across assets. Gold does not participate in the crypto account lock; use a separate demo account/terminal.

Tests use explicit mocks/placeholders, no actual .env/network. Engineering paths are not actual AI results or returns.

Feishu notifications are opt-in through private .env, independent of the trading mode. Never send a real test notification unless the user explicitly requests it; use mocked transport to verify the switch, signing and delivery results.

Manual entry uses crypto-manual-entry-v1: the existing DEMO account owner consumes same-directory/account/asset/symbol/magic commands before dispatching new automatic entry. It waits for in-flight AI rather than starting another MT5 instance. Session-bound requests expire after60seconds; durable claimed commands never automatically replay after crash/ambiguous receipt. Keep all Python hard guards, fixed SL/TP and exact requested volume (or broker risk sizing when omitted). Human intent is an explicit internal source, never a fabricated AI1 proposal/quality card/closed trigger. Persist full original guardian context with exact execution token before broker submission and recover using the existing account ledger. Existing AI3/local guardian and frozen original asset lifetime/cooldown own exits. Feishu uses normal open/close titles with no manual marker or invented confidence/score; unknown user structure remains unknown. Do not add a second guardian loop or a manual-only trading daemon.

AI-1 pool evidence scope is crypto-evidence-scope-v1. The common M15 EMA distance and named common execution-risk leaves are valid evidence. Prompt reference examples must use the same scope and paths that survive compaction. Keep selected candidate/index/direction isolation, reject internal host markers and winner aliases, and require actual non-null paths. Never rewrite an invalid model reply into a trade or resend paid requests to repair a scorecard. On 2026-10-11 the two synced ETH replies became valid no/64 and no/56 after fixing scope, with spread2.0 > .25*M15 ATR; this is not evidence they should open a trade.
