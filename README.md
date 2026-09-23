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

`CPDA_SCANNER_PROVIDER=bingx` (default) or `binance` selects the single exchange for a run: universe, tickers, candles, BTC/ETH context, funding, OI and outcome tracking all come from it. Snapshots and setup episodes record `exchange`; episodes, checkpoints and outcomes never cross exchanges. BingX adapter facts (verified against the live public API):

- Public endpoints, no API key: `/openApi/swap/v2/quote/contracts`, `/quote/ticker` (bulk), `/openApi/swap/v3/quote/klines`, `/quote/premiumIndex` (bulk current funding, next funding time, per-contract interval 1h/4h/8h), `/quote/openInterest` (per symbol), `/openApi/swap/v2/server/time`. The `fundingRate` endpoint returns settled history, so current funding comes from `premiumIndex`.
- Symbols are canonical internally (`BTCUSDT`) and converted only in the adapter (`BTC-USDT`). Tradable = status 1 and `apiStateOpen`; `NCSK/NCFX/NCCO/NCSI` TradFi contracts and USDC contracts are excluded.
- Klines come newest-first with open time only and include the forming candle; close = open + interval − 1 ms and only candles closed before the grace cutoff are used.
- Open interest is **USDT notional**; base quantity is derived as notional ÷ same-cycle mark price. BingX has no public OI history, so OI-change interpretation is unavailable (0 of the 6 "constructive OI" points).
- Funding is classified on an 8h-equivalent rate (`rate × 8 / interval_hours`).
- One shared limiter: 250 requests / 10 s (BingX reports 500), cooldown on 429/418 or rate-limit code 100410, and a pause when `x-ratelimit-requests-remain` is nearly exhausted.

### Scanner Telegram alerts

Required: `CPDA_TELEGRAM_BOT_TOKEN` (bot token, keep it in `.env` only) and `CPDA_SCANNER_TELEGRAM_CHAT_ID` (recipient chat). `CPDA_SCANNER_SEND_TELEGRAM=false` suppresses scanner pushes; with `CPDA_SCANNER_DRY_RUN=true` pushes still go out but are prefixed "🧪 DRY RUN". Portfolio alerts use each portfolio's own chat id and are unaffected.

Pushes follow the existing rules: an ACTIVE setup with score ≥ `CPDA_SCANNER_ALERT_SCORE` (80, i.e. HIGH/EXTREME confluence) alerts once, then again only after `CPDA_SCANNER_COOLDOWN_MINUTES` (60) for a higher state or +`CPDA_SCANNER_SCORE_DELTA` score; a first READY transition of an already-alerted setup bypasses the cooldown once; INVALIDATED/EXPIRED is pushed only for setups that were alerted. WATCH/SETUP_FORMING never push.

```bash
python -m app.telegram.smoke_test           # one connectivity message; prints message_id only
python -m app.telegram.scanner_smoke_test   # injected READY test alert via the real alert path (temp DB)
```

See [implementation and verification notes](docs/scanner-implementation.md) for the file inventory, audit findings and check results.
