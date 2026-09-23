# Setup outcome tracker and calibration dataset

Read-only research. The scanner never places orders. Outcomes describe price paths after a
reference close; they are **setup outcomes**, not trades (no fills, fees, spread, slippage
or funding).

## When an outcome begins

One outcome per `MarketSetup` episode (unique `market_setup_id`), created in the same
transaction as the scanner evaluation that first reports `readiness == READY`:

| Field | Value |
|---|---|
| `ready_at` | close time of the READY 15m candle (closed, confirmed) |
| `entry_reference_price` | that candle's close |
| `invalidation_price` | the setup's structural invalidation at READY (−1R) |
| `initial_risk_distance` | LONG `entry − invalidation`, SHORT `invalidation − entry`; must be > 0 or no outcome starts |
| `target_*_price` | `entry ± k × risk` for k = 0.5, 1, 1.5, 2, quantized to the stored 12 dp |
| `score_at_entry`, `score_breakdown`, `market_regime`, `structure_regime` | copied from the READY evaluation |

Later READY evaluations of the same episode, including after the setup drops out of READY
and returns, do nothing. Changed structure never rewrites stored levels. A new episode
(after invalidation or expiry) can start its own outcome.

Symbol, direction and episode creation time are read from `market_setups`, not duplicated.
`structure_regime` is `TREND` when the 1h macro structure is BULLISH/BEARISH, else `RANGE`.

## Processing

At the end of every scanner cycle, each `TRACKING` outcome advances with candles whose
close time is after its cursor (`last_processed_at`) and not after the cycle time. The
READY candle itself is never evaluated. Candles the scanner already fetched are reused;
other symbols use the provider's incremental closed-candle cache.

For each closed candle, in order:

1. MFE/MAE update from the candle's full HIGH/LOW, in price and in R.
2. Touches: LONG target `high ≥ target`, invalidation `low ≤ invalidation`; SHORT mirrored.
3. Resolution per target (`first_0_5r` … `first_2r`):
   - target only → `TARGET_FIRST` (and `bars_to_*`);
   - invalidation only → every pending target `INVALIDATION_FIRST`;
   - both in one candle → see ambiguity below.
4. Terminal at invalidation, at +2R, or when `bars_processed` reaches the horizon.

A missing candle between the cursor and the next available one ends the outcome as
`DATA_GAP`. It is not a win, a loss or a sample.

### State machine

```
TRACKING ──+2R touched (not ambiguous)──────────────▶ COMPLETED_2R
    │──invalidation touched, order known────────────▶ INVALIDATED
    │──invalidation + target in one candle, unresolved▶ AMBIGUOUS_SAME_BAR
    │──horizon reached (OUTCOME_MAX_BARS_*)─────────▶ EXPIRED (pending targets → UNRESOLVED)
    └──missing candles after the cursor──────────────▶ DATA_GAP
```

Terminal outcomes are frozen. Re-processing the same candles is a no-op (idempotent).

## Same-bar ambiguity

If one candle touches both invalidation and one or more pending targets, the order is
unknown. The tracker then fetches that parent's lower-timeframe candles (15m→5m, 1h→15m,
4h→1h) and uses them only if all of these hold:

- exactly the expected number of children, contiguous, starting at the parent open;
- none closes after the parent's close (only data inside the completed parent);
- the children's max high / min low equal the parent's high / low.

The children are replayed in order. A child touching both sides stays `AMBIGUOUS`; if the
children never reach invalidation (contradicting the parent), nothing is resolved.
`ambiguity_resolution` records `LOWER_TIMEFRAME` or `LOWER_TIMEFRAME_UNUSABLE`. With
`CPDA_OUTCOME_LTF_RESOLUTION=false` or on fetch failure, the candle remains ambiguous.
The tracker never guesses.

## Calibration

Completed outcomes (terminal, excluding `DATA_GAP`) are grouped into score bands
(`CPDA_CALIBRATION_BANDS`, default 60–69, 70–79, 80–89, 90–100; a 0–59 band appears only
if observed). Per band:

- `sample_count`, and `low_sample` when below `CPDA_CALIBRATION_MIN_SAMPLES` (30);
- per target: `target_first / (target_first + invalidation_first)` = **historical observed
  rate**, with the resolved denominator, ambiguous and unresolved counts;
- average/median MFE and MAE in R (MAE ≤ 0);
- `ambiguous_count`, `expired_count`, `data_gap_count`.

Optional grouping: `symbol`, `direction`, `timeframe`, `market_regime`,
`structure_regime`, `asset_class` (BTC/ETH = MAJOR, else ALT). Individual score components
are stored in `score_breakdown` for later analysis but are not yet a grouping key.

## Known biases (read before drawing conclusions)

- Excluding ambiguous and expired-unresolved setups from rates can bias them; both counts
  are always reported next to the rates.
- MFE/MAE include the whole terminal candle, whose extremes may occur after the event.
- The entry reference is a close, not an achievable fill. READY detection can lag the
  candle close by up to one scanner interval.
- Scores and thresholds are unchanged; this dataset is for measurement, not tuning.
