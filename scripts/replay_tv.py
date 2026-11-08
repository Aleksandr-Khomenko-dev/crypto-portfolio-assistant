"""Replay ICT v4.2 webhooks against the local public app (no TradingView needed).

    uvicorn app.public:app --host 127.0.0.1 --port 8001 --forwarded-allow-ips 127.0.0.1
    python -m scripts.replay_tv                # one ENTRY
    python -m scripts.replay_tv --scenario full    # ZONE_WAIT -> ENTRY -> TP1 -> REST_CLOSED
    python -m scripts.replay_tv --scenario cancel  # ZONE_WAIT -> CANCELLED (nothing sent)

Every plan id starts with "TEST-": the bot marks those messages
"TEST — NOT A REAL TRADING SIGNAL". The runner's worker must be running
(CPDA_TV_WEBHOOK_TOKEN set). The request carries X-Forwarded-For with a TradingView
address so the IP allowlist passes; uvicorn honours it only from 127.0.0.1.
The token is read from the settings and never printed.
"""

from __future__ import annotations

import argparse
import time

import httpx

from app.config import get_settings

SCENARIOS = {
    "entry": ["ENTRY"],
    "full": ["ZONE_WAIT", "ENTRY", "TP1", "REST_CLOSED"],
    "cancel": ["ZONE_WAIT", "CANCELLED"],
}
RESULT_R = {"TP1": 0.5, "REST_CLOSED": 0.0}


def payload(event: str, plan_id: str, bar_ms: int) -> dict:
    """The specification's sample SELL on gold, with a fresh bar time."""
    return {
        "event": event,
        "planId": plan_id,
        "symbol": "OANDA:XAUUSD",
        "ticker": "XAUUSD",
        "tf": "5",
        "side": "SELL",
        "model": "Inversion FVG",
        "score": 90,
        "entryType": "market",
        "entry": 3969.80,
        "sl": 3976.80,
        "tp1": 3962.80,
        "tp2": 3959.30,
        "tp3": 3952.00,
        "tp2Name": "London low",
        "tp3Name": "PDL",
        "price": 3969.80,
        "resultR": RESULT_R.get(event),
        "exitPrice": {"TP1": 3962.80, "REST_CLOSED": 3969.80}.get(event),
        "units": 35.714286,
        "riskUsd": 250,
        "leverage": 10,
        "barTime": bar_ms,
        "bornTime": bar_ms - 600_000,
        "validHours": 3,
        "window": "NY morning window",
        "checklist": "✓D ✓4H ✓BSL PDH ✓disp 1.8x ✓window ✓premium ✓DXY",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="entry")
    parser.add_argument(
        "--pause", type=float, default=2.0, help="seconds between events"
    )
    args = parser.parse_args()
    settings = get_settings()
    if not settings.tv_webhook_token:
        raise SystemExit("Set CPDA_TV_WEBHOOK_TOKEN in .env first")
    sender_ip = settings.tv_allowed_ips.split(",")[0].strip()
    plan_id = f"TEST-XAUUSD-5-SELL-{int(time.time())}"
    # The reaction candle closed a few seconds ago (fresh: no "late" warning).
    bar_ms = (int(time.time()) - 300 - 5) * 1000
    with httpx.Client(timeout=10) as client:
        for event in SCENARIOS[args.scenario]:
            reply = client.post(
                f"{args.url}/webhook/tv/{settings.tv_webhook_token}",
                json=payload(event, plan_id, bar_ms),
                headers={"X-Forwarded-For": sender_ip},
            )
            print(f"{event:<12} -> HTTP {reply.status_code}")
            time.sleep(args.pause)
    print(f"plan {plan_id}: check Telegram (only ENTRY is sent) and the trade table")


if __name__ == "__main__":
    main()
