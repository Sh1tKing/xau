# Independent crypto DEMO fork

Source copied from gold runtime7f3df37. Clean public parent00376b31e5b1c0d6b6ac02a408174a276df30898; never push root master history.

Only run.py is operational. Do not import root trader/strategies, read root .env, modify gold files/state, or start terminal/trading/paid AI tests. User configures and starts after review.

Gold SL8 is a gold price distance. Here8USD is bounded account cash loss including configured costs; price distance uses asset ATR/percentage cap. Lots use broker cash calculations and floor to step, never fixed.01. Do not shorten structure/M5 coverage to fit cash/cap. H1, RR1.5, EMA1.5ATR remain hard gates.

USD DEMO only. Observe/dry-run prohibit all order mutations. Entry inspects all account positions; guardian owns exact symbol/magic. Identity switches and unresolved account receipts block entries across assets. Gold does not participate in the crypto account lock; use a separate demo account/terminal.

Tests use explicit mocks/placeholders, no actual .env/network. Engineering paths are not actual AI results or returns.

Feishu notifications are opt-in through private .env, independent of the trading mode. Never send a real test notification unless the user explicitly requests it; use mocked transport to verify the switch, signing and delivery results.
