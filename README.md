# Portfolio Signal Agent

Read-only portfolio intelligence MVP for manually maintained crypto spot portfolios.

## What It Is

`Portfolio Signal Agent` stores your portfolios inside its own database and uses public market data providers only for pricing and signal generation.

The application database is the source of truth for:

- portfolios
- assets
- positions
- cost basis
- signal history
- digest history

External APIs are used only for:

- live price lookup
- market-cap and volume enrichment
- later analytics expansion

## Safety Boundaries

This project is intentionally read-only.

- No wallet integration
- No exchange execution
- No auto-trading
- No brokerage actions
- No fund access

You make real trades outside the app, then manually update positions in the app.

## MVP Scope

- Multiple portfolios
- Portfolio-specific strategy profiles
- Manual portfolio and position management
- Asset registry with provider IDs and trading symbols
- Current price tracking
- Historical price snapshots
- PnL and portfolio-weight calculations
- Abnormal rise and drop detection across 1h / 4h / 24h
- Staged take-profit suggestions
- Concentration and volatility risk alerts
- Telegram bot commands and notifications
- Daily morning digest
- CSV import preview layer for future admin workflows

## Strategy Profiles

### Main Portfolio

- More active monitoring
- Earlier staged take-profit suggestions
- Designed for spot capital growth

Default take-profit guidance:

- `+30%` -> suggest trimming `10-15%`
- `+50%` -> suggest trimming `15-20%`
- `+100%` -> suggest trimming `20-25%`

### Long-Term Kids Portfolio

- More conservative
- Lower alert frequency
- Focus on capital preservation and long-term holding

Default take-profit guidance:

- `+75%` -> suggest trimming `5-10%`
- `+125%` -> suggest trimming `10-15%`
- `+200%` -> suggest trimming `15-20%`

## Market Providers

The provider layer is pluggable.

Current MVP adapters:

- `CoinGecko` primary market adapter
- `Binance` public market fallback adapter
- `Mock` provider for tests

Notes:

- CoinGecko is used through the public/demo API path.
- `CPDA_COINGECKO_DEMO_API_KEY` is optional and free if you want better reliability.
- Binance fallback does not require any API key.
- No paid DropsTab dependency remains in the runtime path.

## Stack

- Python 3.12
- FastAPI
- PostgreSQL
- SQLAlchemy 2.x
- Alembic
- APScheduler
- aiogram 3.x
- httpx
- pydantic-settings
- Docker
- docker-compose

## Architecture

```text
app/
  api/            FastAPI routes and dependencies
  admin_import/   CSV preview parser for future import flows
  db/             Models, sessions, bootstrap, migrations integration
  providers/      Market adapter interfaces and implementations
  scheduler/      APScheduler jobs
  schemas/        Pydantic contracts
  services/       Portfolio CRUD, valuation, signals, digests, alerts
  strategies/     Rule-based decision logic per profile
  telegram/       Bot commands and message formatting
```

Separation rules:

- provider code never owns portfolio data
- business rules never call raw HTTP directly
- Telegram is only a delivery and UX surface

## Core Domain

- `Portfolio`
- `Position`
- `Asset`
- `Transaction`
- `PriceSnapshot`
- `Signal`
- `AlertEvent`
- `DailyDigest`
- `StrategyProfile`
- `UserSettings`

## Local Setup

### Option A: Docker Compose

1. Copy env template:

```bash
cp .env.example .env
```

2. Edit `.env` if needed:

- `CPDA_COINGECKO_DEMO_API_KEY` can stay blank
- `CPDA_TELEGRAM_BOT_TOKEN` can stay blank if Telegram is not needed yet

3. Start the stack:

```bash
docker compose up --build postgres api
```

If you also want the Telegram bot:

```bash
docker compose up --build
```

4. Open API docs:

`http://localhost:8000/docs`

Dashboard:

`http://localhost:8000/`

### Option B: Local Python

1. Create a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -e ".[dev]"
```

3. Copy env template:

```bash
cp .env.example .env
```

By default local Python now points at `sqlite:///./dev.db`, so the dashboard loads the current workspace data immediately.

4. Back up and upgrade the configured database (the helper also validates older unversioned SQLite installs):

```bash
python -m scripts.upgrade_database --adopt-existing
```

5. Start the API:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open the SaaS dashboard:

`http://localhost:8000/`

6. Start the Telegram bot in another terminal if needed:

```bash
python -m app.telegram.bot
```

## API Overview

Base path: `/api`

- `GET /api/health`
- `GET /api/portfolios`
- `POST /api/portfolios`
- `GET /api/portfolios/{portfolio_id}`
- `PATCH /api/portfolios/{portfolio_id}`
- `DELETE /api/portfolios/{portfolio_id}`
- `POST /api/portfolios/{portfolio_id}/positions`
- `PATCH /api/portfolios/{portfolio_id}/positions/{position_id}`
- `DELETE /api/portfolios/{portfolio_id}/positions/{position_id}`
- `GET /api/portfolios/{portfolio_id}/signals`
- `GET /api/signals/{signal_id}`
- `GET /api/portfolios/{portfolio_id}/risk`
- `GET /api/portfolios/{portfolio_id}/digests`
- `POST /api/portfolios/{portfolio_id}/monitor`
- `POST /api/portfolios/{portfolio_id}/digest`
- `POST /api/monitor/run`
- `GET /api/assets`
- `POST /api/assets`
- `PATCH /api/assets/{asset_id}`
- `POST /api/admin/imports/positions/preview`

## Telegram Commands

- `/start`
- `/help`
- `/digest [portfolio]`
- `/portfolio [portfolio]`
- `/signals [portfolio]`
- `/risk [portfolio]`
- `/asset <ticker>`
- `/explain <signal_id>`

Message style is intentionally:

- concise
- trader-friendly
- mobile-readable
- action-oriented
- explicit about reasoning
- explicit about risk

## Example Portfolio Create Payload

```json
{
  "name": "Main Portfolio",
  "strategy_profile_code": "main",
  "positions": [
    {
      "quantity": "0.5",
      "average_entry_price": "62000",
      "asset": {
        "symbol": "BTC",
        "name": "Bitcoin",
        "coingecko_id": "bitcoin"
      }
    },
    {
      "quantity": "3",
      "average_entry_price": "2800",
      "asset": {
        "symbol": "ETH",
        "name": "Ethereum",
        "coingecko_id": "ethereum"
      }
    }
  ]
}
```

## Testing

Run tests with:

```bash
pytest
```

Current suite covers:

- portfolio CRUD API
- CSV preview API
- monitoring and digest generation
- strategy profile take-profit behavior

## Important Note For Existing Local Installs

If you already have an older `.env`, merge new values from `.env.example` while preserving your database URL and credentials.

The configuration changed from the older DropsTab-based version to a DB-first, provider-adapter architecture.

## Market-wide futures scanner

The portfolio application now also scans public USDT perpetual markets. It remains read-only: no exchange credentials, orders, wallets or transfers. An **85/100 score means 85 points of model confluence**, never an 85% win probability. Even a high score can require a retest or fail the risk filter.

- Universe: trading crypto perpetual contracts, minimum $10m quote volume, top 100 by default, whitelist/blacklist and hard market cap.
- Closed 15m/1h/4h candles: EMA8/21/50/200, Wilder ATR/RSI/ADX/DI, completed-bar RVOL, confirmed macro/micro pivots, BOS/CHoCH, ATR zones, FVG mitigation, sweeps and retests.
- Public funding/OI plus aligned price/OI changes and BTC/ETH market context. Missing, failed or stale metrics receive no credit.
- Separate LONG/SHORT scores and readiness states, structural invalidation, observed targets and minimum 1.5 R:R.
- Historical snapshots, setup lifecycle, durable notification cooldown and numerical rankings.
- Existing dashboard now has a **Scanner** tab. No frontend framework was added.

Scoring caps: regime 15, structure 20, location 15, trigger 15, volume/momentum 10, derivatives 10, BTC/ETH 5, risk/room 10. Correlated observations share a block. Default score bands: IGNORE <60, WATCH 60–69, SETUP_FORMING 70–79, HIGH_CONFLUENCE 80–89, EXTREME_CONFLUENCE 90–100. Readiness is independently WAIT_LOCATION, WAIT_STRUCTURE, WAIT_RETEST, WAIT_VOLUME, WAIT_RISK or READY.

All configuration uses the existing **`CPDA_` prefix**. The complete scanner settings are in `.env.example`. Key defaults:

```dotenv
CPDA_SCANNER_ENABLED=true
CPDA_SCANNER_INTERVAL_MINUTES=5
CPDA_SCANNER_TOP_N=100
CPDA_SCANNER_MAX_MARKETS=100
CPDA_SCANNER_MIN_QUOTE_VOLUME_USD=10000000
CPDA_SCANNER_MIN_RR=1.5
CPDA_SCANNER_USE_BTC_CONTEXT=true
CPDA_SCANNER_USE_DERIVATIVES=true
CPDA_FVG_MIN_ATR=0.15
CPDA_SCANNER_TELEGRAM_CHAT_ID=
```

Set `CPDA_SCANNER_TELEGRAM_CHAT_ID` and the existing bot token to enable scanner alerts. Polling is optional for delivery. The scanner does not infer recipients from portfolio chats. `/scanner`, `/toplong`, `/topshort`, `/setup <symbol>` and `/watchlist` read persisted analyses; all existing portfolio commands remain available. `/watchlist` means current qualifying scanner setups, not a separately editable user watchlist.

New endpoints:

```text
GET  /api/scanner/status
GET  /api/scanner/setups?minimum_score=70&direction=LONG&state=SETUP_FORMING&symbol=BTCUSDT
GET  /api/scanner/setups/{symbol}
GET  /api/scanner/snapshots
POST /api/scanner/run
GET  /api/scanner/top/long
GET  /api/scanner/top/short
POST /api/webhooks/tradingview
```

Rankings contain current active episodes; symbol detail and snapshots also retain low-scoring and stale research results with timestamps. The optional TradingView endpoint is disabled by default. Enabling it requires a 24+ character `CPDA_TRADINGVIEW_WEBHOOK_SECRET`; the validated JSON payload carries `secret`, `event_id`, `symbol`, `timeframe`, `event`, `direction`, `timestamp`, `price`, and optional numeric `value`. Events must be within five minutes. Secrets are not persisted. Events are stored for future research and **do not affect scores**. See `/docs` for schemas.

### Run locally against an existing SQLite portfolio

Use Python 3.12 or newer. Preserve your existing `.env`; add settings from the example as needed.

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
export CPDA_DATABASE_URL=sqlite:///./dev.db
python -m scripts.upgrade_database --adopt-existing
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

The upgrade helper makes a timestamped SQLite backup. If an existing SQLite database has no Alembic revision, `--adopt-existing` validates its tables, columns, types, keys and uniqueness against the pre-scanner baseline before stamping it. Unknown schemas are rejected. For an already versioned PostgreSQL database or a new empty database, run `alembic upgrade head`. Migration `20260923_0004_scanner` adds four tables and does not alter portfolio rows.

```bash
curl -X POST http://127.0.0.1:8000/api/scanner/run
curl http://127.0.0.1:8000/api/scanner/top/long
pytest
pytest tests/test_scanner_analytics.py tests/test_scanner_scoring.py tests/test_scanner_provider.py tests/test_scanner_service.py tests/test_scanner_migrations.py
```

The test commands use deterministic fixtures and mocked HTTP; they do not call Binance or send Telegram messages. For a fresh Docker install, existing `docker compose up --build postgres api` applies migrations automatically.

Run **one scheduler/API worker**: cache and overlap protection are process-local. The standalone bot reads the same database. Initial downloads can take several minutes; subsequent cycles reuse closed candles and request only missing history. Provider calls use bounded concurrency, pacing and retry/backoff; each failed market is isolated. CPU analytics run in worker threads. Existing portfolio jobs remain separate.

### Research limitations

These are configurable heuristics, not calibrated probabilities or profitability claims. Pivot confirmation deliberately lags; EMA initialization uses an SMA seed and finite history. R:R uses observed zones and excludes fees, spread, slippage and funding costs. New markets without 210 closed bars on each timeframe are skipped. Zone interactions are descriptive, not an automatic strength score. OI relationships are possible interpretations, not proof of trader intent.

On-chain, macro and news/fundamental providers are explicitly unavailable. No position sizing, execution simulation, distributed locking or scanner-history retention policy is implemented. Setup outcomes (below) measure price paths from a reference close; they are not trades. Funding is a fractional per-interval value; OI is base-asset quantity, not USD. A current ticker is stored separately from the confirmed 15m reference price. Setup episodes pin their original invalidation and expiry; later snapshots preserve newly calculated risk plans. Invalidation currently requires a closed 15m price through the episode's original level.

The public adapter follows [Binance USDⓈ-M market-data documentation](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data). Regional API availability and live delivery remain environment-dependent; validation uses mocks.

## Setup outcome research (calibration dataset)

Every setup episode that first reaches `READY` on a closed 15m candle starts one outcome record. Entry reference = that candle's close; −1R = the invalidation frozen at READY; +0.5R/+1R/+1.5R/+2R targets are stored at creation and never recomputed. Each later closed candle's high/low is evaluated until invalidation, +2R or the bar horizon. A candle touching both a target and invalidation is `AMBIGUOUS` unless complete 5m candles inside it resolve the order; ambiguous and expired setups are excluded from rates and reported as counts.

Calibration groups completed outcomes by score band (60–69, 70–79, 80–89, 90–100) and reports **historical observed rates** — never probabilities. Bands below `CPDA_CALIBRATION_MIN_SAMPLES` are flagged as too small. Scoring weights are unchanged by this data.

- API: `GET /api/research/outcomes`, `/api/research/calibration[?group_by=direction,asset_class]`, `/api/research/calibration/{symbol}`, `/api/research/setup/{setup_id}/outcome`; filters `direction`, `symbol`, `score_min`, `score_max`, `regime`, `timeframe`.
- Telegram (on demand only): `/performance`, `/performance ARB`, `/calibration`.
- Dashboard: Scanner tab → Research · Score calibration.

### 24/7 dry-run collection

```bash
python -m alembic upgrade head          # or: python -m scripts.upgrade_database
export CPDA_SCANNER_DRY_RUN=true CPDA_SCANNER_SEND_TELEGRAM=false
python -m app.scanner.runner --once     # smoke test: one cycle
mkdir -p logs && nohup python -m app.scanner.runner >> logs/scanner.log 2>&1 &
```

The runner uses public Binance USDⓈ-M endpoints only, runs just the scanner and outcome tracker (no portfolio jobs, no bot polling), logs per-cycle telemetry and stops cleanly on SIGINT/SIGTERM. Do not run it alongside the API scheduler against the same database; the scan lock would make one of them skip cycles. Definitions: [outcome tracker notes](docs/outcome-tracker.md).

### Exchange provider (BingX by default)

`CPDA_SCANNER_PROVIDER=bingx` (default) or `binance` selects the exchange for **new scans only**. Outcome tracking uses the exchange stored on each setup (a provider registry keeps one long-lived provider per exchange), so switching `bingx → binance → bingx` never interrupts or re-routes an open outcome. A scan run uses a single exchange for: universe, tickers, candles, BTC/ETH context, funding and OI. Snapshots and setup episodes record `exchange`; episodes, checkpoints and outcomes never cross exchanges. BingX adapter facts (verified against the live public API):

- Public endpoints, no API key: `/openApi/swap/v2/quote/contracts`, `/quote/ticker` (bulk), `/openApi/swap/v3/quote/klines`, `/quote/premiumIndex` (bulk current funding, next funding time, per-contract interval 1h/4h/8h), `/quote/openInterest` (per symbol), `/openApi/swap/v2/server/time`. The `fundingRate` endpoint returns settled history, so current funding comes from `premiumIndex`.
- Symbols are canonical internally (`BTCUSDT`) and converted only in the adapter (`BTC-USDT`). Tradable = status 1 and `apiStateOpen`; `NCSK/NCFX/NCCO/NCSI` TradFi contracts and USDC contracts are excluded.
- Klines come newest-first with open time only and include the forming candle; close = open + interval − 1 ms and only candles closed before the grace cutoff are used.
- Open interest is **USDT notional**; base quantity is derived as notional ÷ same-cycle mark price. BingX has no public OI history, so the scanner **records its own**: each cycle's live OI (base quantity) is stored in `open_interest_snapshots` for the nearest 15m close boundary, only if observed within `CPDA_OI_SNAPSHOT_TOLERANCE_SECONDS` (150 s) of it, one row per exchange/symbol/boundary (the observation closest to the boundary wins; re-writes are no-ops), pruned after `CPDA_OI_SNAPSHOT_RETENTION_DAYS` (14). Nothing is back-filled or interpolated: OI change 15m/1h/4h (`oi_change_by_horizon`, anchored at the latest closed boundary) is `null` until both endpoints were really observed, and the existing constructive-OI points are only possible once two consecutive real boundaries exist. Expect ~15 min before the first 15m delta, 1 h / 4 h for the longer horizons, and gaps after downtime.
- OI is sampled by a **dedicated collector**, not the technical scan: at every wall-clock boundary (:00/:15/:30/:45, recomputed from the clock each time, so a restart at 10:07 targets 10:15 and nothing is back-filled) it fetches the current universe's OI with bounded concurrency through the scanner's own BingX provider (same cache, semaphore and rate budget, with a short priority window). It downloads no candles or funding and runs no scoring; mark prices for the base-unit conversion come from one short BingX `@markPrice` WebSocket batch, accepted only if stamped between the boundary and the moment of use. Collection stops at the tolerance deadline; late or unaligned readings are counted, never stored. Network calls finish before one short write transaction (atomic `ON CONFLICT` upsert). The scanner only reads stored history (as of its own observation time) plus live funding. It runs in `python -m app.scanner.runner` and in the API scheduler; `GET /api/scanner/oi-collection` shows the last report, including `oi_collection_requested/successful/failed/late/duplicates/elapsed_seconds` and diagnostic 15m/1h/4h coverage rates (not used in scoring).
- Funding is classified on an 8h-equivalent rate (`rate × 8 / interval_hours`).
- One shared limiter: 250 requests / 10 s (BingX reports 500), cooldown on 429/418 or rate-limit code 100410, and a pause when `x-ratelimit-requests-remain` is nearly exhausted.

### News context (increment 1; context only)

News never changes a trading score, threshold, alert rule, risk plan, OI logic or outcome. It runs as an event-driven pipeline beside the scanner (`app/news/`): providers → bounded priority queue (CRITICAL first, backpressure) → entity resolution (`config/news_projects.toml`: public project metadata, Tier 1/2 = monitoring priority, never a rating; portfolio priority is read from the database) → deterministic noise filter, event classification and diagnostic NEWS_IMPORTANCE (0–100, severity-gated; CRITICAL ≥ 90, HIGH ≥ 75, MEDIUM ≥ 55) → cross-source clustering (official source canonical; verification UNVERIFIED / SINGLE_SOURCE / PRIMARY_CONFIRMED / MULTI_SOURCE_CONFIRMED) → append-only storage (`news_*` tables, migration 0008) → links to active setups → Telegram.

- Sources today (free, verified live): BingX official announcements (REST poll, shares the scanner's BingX rate limiter) and CFTC press RSS; SEC press RSS once `CPDA_NEWS_SEC_USER_AGENT` is set. None offers push, so upstream latency is bounded by the poll interval (30–60 s). A generic streaming transport (reconnect/backoff, heartbeat, stale watchdog, replay dedup) is ready for push feeds (e.g. Binance announcements, which need a read-only API key; paid feeds after their licences are documented).
- Telegram: `📰 НОВЫЙ КОНТЕКСТ ДЛЯ АКТИВНОГО СИГНАЛА` follow-ups are threaded under the setup's first alert; CRITICAL events can send `🚨 СРОЧНАЯ НОВОСТЬ` (or `⚠️ ПЕРВИЧНОЕ СООБЩЕНИЕ` → `✅ НОВОСТЬ ПОДТВЕРЖДЕНА`) with a per-symbol cooldown; setups are never invalidated by news. Scanner alerts include up to 3 events received before the scan time. Alerts are claimed in the DB before sending (at most once), items older than 60 min (CRITICAL 4 h) at receipt are stored but never alerted.
- Scanner alerts are now sent as each symbol's closed-candle result is persisted (not after the whole scan), and a lifecycle tick (`CPDA_LIFECYCLE_CHECK_SECONDS`) sends expiries between scans; notification rules and cooldowns are unchanged.
- `GET /api/news/status` (p50/p95/p99 per stage, queue depth, per-provider SLO REALTIME_OK / DEGRADED / STALE) and `GET /api/news/{symbol}` (TradingView `news_provider`-shaped JSON).
- Not yet: market-reaction observations, macro calendar, theme graph, commodities, paid providers.

### Fast market watcher (early warning; not a trading score)

`app/fast/` runs beside the closed-candle scanner (independent of the 5-minute scan, 15m closes, OI boundaries and news). It streams `{SYMBOL}@kline_1m` for the liquid BingX universe (≥ `CPDA_FAST_MIN_QUOTE_VOLUME_USD`; portfolio/active-setup symbols at a lower floor) over multiplexed public WebSocket connections (BingX has no all-market stream), keeps 15s/30s/1m/3m/5m price windows and running 1-minute volume, and triggers only when a move exceeds both an absolute floor and `k × σ` of that asset's own recent 1-minute volatility (baseline: last 60 closed 1m candles). States: FAST_MOVE → CONFIRMED_MOMENTUM (volume ≥ 3× normal) → EXTREME_MOVE. Alerts (`⚡ РЕЗКОЕ ДВИЖЕНИЕ`, `🔥 ИМПУЛЬС ПОДТВЕРЖДАЕТСЯ`, `⚡ ДВИЖЕНИЕ ПО АКТИВНОМУ СЕТАПУ`) carry a TradingView button, show the last closed-candle technical score read-only, and are sent once per symbol+direction episode, again only on upgrade, 50% extension or a new confirmation, with its own 30-minute cooldown. Each event requests a closed-candle PRIORITY RESCAN of that one symbol (`ScannerService.run(only=...)`); normal ≥80 alerts still come only from the normal rules. Events are stored in `fast_market_events` (migration 0009).

### Early structural layer (zones, ignition, formations, breakout/retest)

`app/early/` shares the fast watcher's stream and outbox. Every 5m close it builds a CLOSED-candle context per symbol: 15m/1H/4H zones and previous breakout levels come from the scanner's own S/R engine (its in-memory closed-candle frames, read-only, or the same `analyze_frame` on cached candles), plus a 5m frame (BOS/CHoCH, pivots, RVOL) and deterministic formation candidates (compression at level, ascending/descending triangle, bull/bear flag, sweep + reclaim, breakout compression). Realtime prices then drive the event state machine: `ZONE_WATCH`, `BULLISH/BEARISH_IGNITION` (structural shift required; `ignition_strength` 0–100), `FORMATION_WATCH`, `BREAKOUT_APPROACH` (≤0.35 ATR, arrived from ≥0.75 ATR), `FIRST_BREAK` (intrabar; penetration ≥ max(0.10 ATR, 2σ₁ₘ, 3×spread) held ≥10 s over ≥3 updates, transition seen live), `RETEST_WATCH`, `RETEST_CONFIRMED` (no deep return + ≥2 of micro BOS 1m, 5m higher low, volume, OI holding, BTC/ETH compatible), `FAILED_BREAKOUT`, `MOMENTUM_CONFIRMED`, `LATE_EXTENDED_MOVE`. Late-move protection: an event whose price is >3 × 1H ATR from the move's origin (lowest low / highest high of the last 4h, reset by ≥1h of accepted trading at the current level) is relabelled `⚠️ ДВИЖЕНИЕ УЖЕ РАСТЯНУТО` and never called early. Strengths are diagnostics, never probabilities; nothing here changes the technical score, thresholds, readiness, risk or scanner cooldowns. Detection dedupe: once per symbol + direction + event + episode key, one material upgrade (see the notification policy below for what is sent); the first context pass after a restart primes state silently and recent events/open episodes are restored from the DB. Tables (migration 0010): typed rows in `fast_market_events`, `market_structure_episodes`, `pattern_candidates`, `early_event_outcomes` (outcomes use only candles after the event). `GET /api/scanner/early/events`, `GET /api/scanner/early/calibration` (rates withheld below `CPDA_CALIBRATION_MIN_SAMPLES`). Synthetic TEST run (own throwaway DB): `python -m scripts.early_synthetic_test /tmp/x.db [--send]`.

### Early-warning notification policy (detection ≠ notification)

Every valid early event is detected, persisted (`fast_market_events.context.notify` records `eligible` and the suppression reason) and measured for outcome research; Telegram is deliberately selective (`app/early/policy.py`):

| Event | Telegram |
|---|---|
| RETEST_CONFIRMED, FIRST_BREAK, RETEST_WATCH | always (once per breakout episode; never rate limited) |
| MOMENTUM_CONFIRMED, EXTREME_MOVE | once (EXTREME skipped if a break/retest of that symbol was just shown) |
| BULLISH/BEARISH_IGNITION | only at a 1H/4H zone, with a fresh shift and volume/RVOL, OI (stable/expanding) or sweep+reclaim confirmation |
| BREAKOUT_APPROACH | only ≤ 0.20 ATR (`CPDA_EARLY_NOTIFY_APPROACH_ATR`) with a supporting condition; wider ones (≤ 0.35) are research |
| FAILED_BREAKOUT | only if the user saw that breakout (FIRST_BREAK/RETEST) |
| FAST_MOVE | only at/near 1H/4H structure, on an active setup, or with a fresh break |
| ZONE_WATCH, FORMATION_WATCH, LATE_EXTENDED_MOVE | never by default (research/context) |

Related events at one level (e.g. triangle + compression + approach, or a pattern boundary and an S/R zone broken together) form one user-facing episode: a lower/equal stage already shown is suppressed as a duplicate, and follow-ups reply under the episode's first message (or the active setup's alert). Ignition/approach share a smoothing budget (3 per 5 min, 12 per hour). The watcher status log reports per event type: `detected`, `persisted`, `telegram_eligible`, `sent`, `suppressed_by_policy`, `suppressed_duplicate`, `suppressed_late`, `suppressed_rate_limit`, and `redetections` (the same event seen again on later ticks).

### Host sleep and clock

Nothing runs while the host sleeps: on macOS idle sleep the realtime WebSocket, the scanner, the OI collector and news processing all pause, and resume on wake (stale contexts are ignored, open episodes are restored, missed OI boundaries are not backfilled). For true 24/7 operation deploy the runner on an always-on host. Keep the host clock NTP-synced; OI validation tolerates only `CPDA_OI_CLOCK_SKEW_TOLERANCE_SECONDS` (2 s) of exchange-ahead skew.

### Scanner Telegram alerts

Required: `CPDA_TELEGRAM_BOT_TOKEN` (bot token, keep it in `.env` only) and `CPDA_SCANNER_TELEGRAM_CHAT_ID` (recipient chat). `CPDA_SCANNER_SEND_TELEGRAM=false` suppresses scanner pushes; with `CPDA_SCANNER_DRY_RUN=true` pushes still go out but are prefixed "🧪 DRY RUN". Portfolio alerts use each portfolio's own chat id and are unaffected.

Pushes follow the existing rules: an ACTIVE setup with score ≥ `CPDA_SCANNER_ALERT_SCORE` (80, i.e. HIGH/EXTREME confluence) alerts once, then again only after `CPDA_SCANNER_COOLDOWN_MINUTES` (60) for a higher state or +`CPDA_SCANNER_SCORE_DELTA` score; a first READY transition of an already-alerted setup bypasses the cooldown once; INVALIDATED/EXPIRED is pushed only for setups that were alerted. WATCH/SETUP_FORMING never push.

```bash
python -m app.telegram.smoke_test           # one connectivity message; prints message_id only
python -m app.telegram.scanner_smoke_test   # injected READY test alert via the real alert path (temp DB)
```

See [implementation and verification notes](docs/scanner-implementation.md) for the file inventory, audit findings and check results.
