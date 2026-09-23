# Scanner implementation and verification

## Audit and integration

The existing FastAPI routers use synchronous SQLAlchemy sessions and async market providers. `AbstractMarketDataProvider` serves asset UUID-based spot portfolio quotes; it is preserved. The new exchange-neutral `FuturesProvider` supplies contracts, tickers, OHLCV and derivatives without coupling market scanning to portfolio positions. Pure analytics and scoring contain no HTTP calls. Existing portfolio models/signals remain in place; scanner history has no portfolio foreign key.

`SignalService`'s persisted cooldown pattern is extended into scanner episode notification state. API and APScheduler share one process-local runtime/provider/cache/lock. Telegram only reads stored results and formats/delivers them. The existing static dashboard gains a tab.

Baseline issues found and addressed:

- `app/telegram/` was missing and prevented test collection/startup. Its four original source modules were recovered from the saved local coverage HTML, preserving portfolio commands. Scanner handlers are separate. The bot module now has its executable module entry point.
- Extreme-pump handling skipped independent take-profit/risk checks. It now suppresses only duplicate abnormal-rise alerts.
- The digest title differed from the existing test contract; the compatible title is restored, and the formatter accepts both forms.
- Migration 0002 executed PostgreSQL-only enum SQL on SQLite. It now skips that SQL on SQLite.
- Existing model index/uniqueness declarations differed from migration 0001. Metadata now matches the established schema; no portfolio rows or indexes are rewritten by scanner migration 0004.
- Local `dev.db` has an empty Alembic version table. Its schema passed read-only baseline validation. The supplied guarded upgrade helper backs up and validates before explicit adoption. The working portfolio DB was not migrated during development.

## Files added

- `app/analytics/`: `analysis.py`, `technical.py`, `structure.py`, `levels.py`, `derivatives.py`, `market_context.py` and package initializer.
- `app/scanner/`: `domain.py`, `universe.py`, `scoring.py`, `risk.py`, `repository.py`, `notifications.py` and package initializer.
- `app/providers/futures.py`, `app/providers/binance_futures.py`.
- `app/services/scanner_service.py`, `app/db/scanner_models.py`.
- `app/api/routers/scanner.py`, `app/api/routers/tradingview.py`.
- `app/telegram/scanner.py`, `app/telegram/scanner_handlers.py`; restored `__init__.py`, `bot.py`, `handlers.py`, `formatters.py`.
- `alembic/versions/20260923_0004_scanner.py`.
- `scripts/upgrade_database.py`.
- `tests/scanner_fixtures.py`, five `tests/test_scanner_*.py` modules.
- This implementation note.

## Existing files changed

`app/config.py`, `app/api/deps.py`, `app/api/routes.py`, `app/db/models.py`, `app/scheduler/jobs.py`, `app/services/telegram_service.py`, `app/services/digest_service.py`, `app/strategies/base.py`, `app/static/dashboard.html`, migration `20260404_0002_add_extreme_pump_pullback_signal_types.py`, `.env.example`, `README.md`.

## Database and research data

Migration `20260923_0004_scanner` follows `20260406_0003_price_alerts` and adds:

- `scanner_runs`: run timing, counts, safe errors and scanner configuration (no tokens or chat destinations).
- `scanner_snapshots`: both scores, per-block points, reference and observed prices, candle timestamps, timeframe analytics, zones/FVGs, derivatives, BTC/ETH context and model version.
- `market_setups`: active episode state, current score/readiness, original invalidation, fixed expiry and durable last-successful notification baseline.
- `tradingview_events`: validated supplemental events, excluding their authentication secret.

History is retained for later R-multiple outcome analysis. There is no claimed win rate, expectancy or predictive probability. Exactly-once Telegram delivery is not guaranteed if the process dies after Telegram accepts a message but before the DB records success.

## Validation

Deterministic tests cover all requested analytics, scoring bounds/direction/readiness, universe selection, closed-bar filtering, missing/stale/gapped data, public adapter parsing/cache/retries, partial provider failures, run locking, persistence, deduplication/expiry/invalidation, Telegram commands/formatting, API filtering and optional webhook validation. Migration tests preserve a portfolio asset through upgrade/downgrade and exercise guarded SQLite adoption.

The full portfolio and scanner suite passes 68 tests with 76.10% coverage, exceeding the existing 60% gate. Temporary SQLite and PostgreSQL 16 databases verified upgrade, scanner downgrade/re-upgrade and `alembic check`. PostgreSQL also passed application lifespan startup and an end-to-end mocked scanner API cycle. The available local interpreter is Python 3.13; project syntax remains Python 3.12-compatible and existing CI targets 3.12.

Scanner modules (`app/scanner`, `app/analytics`, the futures provider/request budget and scanner service) pass Ruff lint, Ruff formatting and mypy. Repository-wide checks were also run and still report pre-existing issues, including legacy dashboard schema/service mismatches, missing CoinGecko UUID typing, FastAPI default dependency lint findings under the inherited Ruff configuration, and unformatted legacy files. These unrelated modules were not broadly refactored.

No live exchange query or Telegram delivery was required for validation. No new exchange credentials or execution capabilities were introduced.

## Self-review fixes

- Duplicate requests: funding comes from one batched `premiumIndex` request (weight 10) per cache window instead of one request per market; expired time-bucketed cache keys are pruned.
- Rate limits: request weights follow Binance's published per-endpoint/`limit` tiers; a cooldown longer than 120 s (e.g. a 418 ban) fails the cycle instead of holding the scan lock for hours.
- Closed candles: a revised closed candle now drops that symbol's bar cache, so the next cycle rebuilds instead of failing forever on the same cached bar. The stale check tolerates the 1 ms close-time offset.
- Repainting/checkpoints: a structure/FVG checkpoint older than the supplied history (downtime longer than the window, or a cache rebuild) now triggers a causal cold rebuild instead of failing that frame on every cycle.
- FVGs: carried gaps are bounded to the analysis window, matching a cold rebuild and preventing unbounded growth.
- Zones: interaction counts include the pivot bar's own touch.
- Transactions: each market's persistence commits separately; the long outer transaction with savepoints (fragile on pysqlite) is gone, and earlier snapshots survive a later error. The full-history snapshot seed query runs once per process, not every cycle.
- Telegram: repeated delivery failures back off exponentially (from the third failure, up to 6 h), so a permanently rejected message cannot occupy per-run alert slots every cycle.
