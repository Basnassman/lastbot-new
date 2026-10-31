# cBot integration: `POST /trade-result`

## Repository status

There is no cBot/C# source in this repository. This is an integration specification only; no cBot behavior has been changed or exercised here. The Brain endpoint accepts query parameters and preserves the legacy request containing only `pnl_usd`.

## Contract

Current compatible request:

```http
POST /trade-result?pnl_usd=-12.50
```

Dedup-enabled request (the current endpoint takes these as query parameters):

```http
POST /trade-result?pnl_usd=-12.50&deal_id=123456789
```

Equivalent client payload data before query encoding:

```json
{
  "pnl_usd": -12.50,
  "deal_id": "123456789"
}
```

`deal_id` is optional for backward compatibility. The account scope is taken from Brain's `CTRADER_ACCOUNT_ID`; the client must not choose or override the account scope. `pnl_usd` and `deal_id` are endpoint query parameters, not a JSON request body in the current API. Use the account's final net PnL converted to USD if cTrader reports another account currency. `deal_id` must be passed as an invariant-culture decimal string (the API's SQLite key is textual).

## cTrader identity source to use

Use cTrader Automate's closed-trade history, not `Position.Id`:

- In cTrader Automate API 5.1+, `HistoricalTrade.ClosingDealId` is documented as the unique closing-deal identifier; `HistoricalTrade.ClosingDeal.Id` is the corresponding Deal ID (`long`). Convert it to invariant-culture decimal text.
- `HistoricalTrade.NetProfit` is the final trade PnL including swaps and commissions; confirm the account currency and USD conversion semantics for the broker/account before sending it as `pnl_usd`.
- cTrader's official `HistoricalTrade` API example handles `Positions.Closed` and looks up history by `args.Position.Id`. That Position ID is only a lookup key, not the dedup ID. Resolve the corresponding final history record and send its closing deal ID.
- A position can have more than one closing deal (for example, partial closes). Send one final result per distinct closing deal, each with its own unique closing Deal ID, and verify that the history record's PnL is scoped to that closing amount rather than the whole position before deployment.

References: [HistoricalTrade](https://help.ctrader.com/ctrader-algo/references/Trading/History/HistoricalTrade/) and [Deal](https://help.ctrader.com/ctrader-algo/references/Trading/Deals/Deal/).

## Required cBot behavior outside this repository

1. Select one close-processing path. Wait until the closing deal/history entry is final and its PnL is authoritative; do not report a still-open or merely requested close.
2. Build one payload per closing Deal ID. Do not additionally report it from another close callback/polling path.
3. On timeout or retryable HTTP failure, resend the exact same `deal_id` and PnL; never mint a retry ID. For an applied-but-response-lost timeout, the retry should return `duplicate`. Keep retrying an event only when the response explicitly indicates audit-store unavailability; unresolved/application errors require operator escalation.
4. Treat `200 recorded` as applied and `200 duplicate` as already handled. A `503 trade_result_unresolved` is not success: stop automatic retries that could change payloads, retain the exact event and alert an operator. Retry `503 audit_store_unavailable` with the same deal ID. A `500 risk_update_unresolved` also requires operator review; Brain intentionally does not reapply ambiguous errors automatically.
5. The legacy client that sends only `pnl_usd` remains accepted but has no reliable deduplication on timeout/retry. For any deal-ID request (in demo or live), Brain requires a positive numeric account ID; `live` also requires it for legacy requests. `CTRADER_ENVIRONMENT` must be exactly `demo` or `live` at request handling time.

`pnl_usd` values that reach the endpoint but are non-numeric or non-finite (NaN/Infinity) are recorded as `rejected` when SQLite is available; only a bounded PnL string is stored, never request headers or credentials. Missing or structurally invalid parameters rejected by FastAPI before entering the endpoint may not be recorded in SQLite.

## Manual unresolved-record procedure

Run the read-only report with `python -m scripts.report_unresolved_trade_results` (or `--db PATH`). It lists `event_id`, deal/account, PnL, received time, status, `risk_manager_updated`, and any stored error/application timestamp details; it opens SQLite in read-only mode and does not create a database or mutate rows. The data is not exposed through an unauthenticated HTTP administration route. `claimed` means the durable reservation exists and the manager call has not started; `applying` means it was about to call the in-memory manager and remains ambiguous after a crash; `error` is ambiguous (including exceptions after an in-memory update); `orphaned` is an operator classification, not proof of whether PnL was applied. `applied` records that the manager call returned normally at that time; `risk_manager_updated=1` is also stored when finalization failed after a normal return. Neither means the in-memory counter survives a process restart. The report does not infer or reconcile the counter. No automatic restart promotion marks old rows as `orphaned`; unresolved rows remain `claimed`, `applying`, or `error` unless an explicitly reviewed administrative action changes the classification.

An authorized operator must compare the exact deal with the broker's authoritative history and any surviving Brain process/log evidence, assess the current day's in-memory counter and restart boundary, then decide whether it should remain unresolved or be classified/reconciled. Any status change or PnL application must be a separate, reviewed administrative action with a recorded operator, evidence, and decision; do not replay unresolved rows wholesale. Current code deliberately provides no automatic or ad-hoc reconciliation endpoint; `orphaned` must only be assigned through a separately reviewed audit procedure that preserves the decision history. The current read-only report includes legacy `orphaned` rows if present; no code in this change creates or resolves them. No reconciliation is implemented or run by this task. Restarting Brain starts its existing in-memory daily counter at zero; this task deliberately does not reconstruct it from SQLite because that could change daily-loss behavior.

## cBot verification still required

Before enabling the integration, inspect the actual cBot and installed cTrader Automate API version to verify partial-close history semantics, uniqueness/stability of `ClosingDealId`, final USD PnL calculation, and that only one reporting path runs per closing deal. Then run a demo-account integration test with no production execution. This repository cannot perform those checks without the cBot source and an authorized demo integration environment.

## Daily metric: signed net realized PnL

`RiskManager.register_trade_result` now adds the signed value (`+profit`, `-loss`) to a single in-memory daily net PnL. The daily-loss circuit breaker in `RiskManager.evaluate` fires only when net realized PnL is at or below the negative limit `ACCOUNT_BALANCE_USD * MAX_DAILY_LOSS_PERCENT / 100`, and a later profit lowers the net and re-enables trading. No risk setting, sizing rule, threshold, indicator or voting weight was changed. The limit is still derived from the static configured balance, not from live account equity.

## Deal identity: current coverage and limitation

There is no cBot/C# source in this repository, so the client side cannot be changed here. Brain's dedup key is `(account_scope, deal_id)`; `deal_id` is supplied entirely by the cBot and must be the closing deal's `HistoricalTrade.ClosingDealId` (not `Position.Id`), one value per closing deal so partial closes stay distinct. Brain cannot derive this identifier from price/candle data and must not synthesize one or deduplicate by PnL value.

Operational evidence from the audit store (2026-10-30 snapshot): 252 of 252 rows have `deal_id = ''` and `dedup_applied = 0`. Deduplication has therefore **never** been active in production. A retry or a second close callback for the same closing deal is applied to the daily net PnL again. The endpoint now emits a warning for every accepted result without `deal_id` (event id only, no PnL) so this state is visible in logs, but it does not reject the request: rejecting would silently drop real PnL. Exactly-once processing is **not** guaranteed until the cBot sends `ClosingDealId` for every result.

## Restart restore: assessment only (not implemented)

Restoring the daily net PnL counter from `applied` rows after a restart is technically possible but is deliberately **not** part of this fix. A correct restore needs: (1) an explicit day key and timezone (broker day vs UTC vs process-local `date.today()`, which currently differ); (2) a decision on `error`/`applying` rows that may or may not have mutated the old in-memory counter; (3) an atomic handoff so a second process cannot apply the same row while the first is alive; and (4) a stored counter snapshot rather than a re-sum that could double count. Doing any of this in the current process-local design would change daily-loss behavior and belongs in a separate, reviewed change.
