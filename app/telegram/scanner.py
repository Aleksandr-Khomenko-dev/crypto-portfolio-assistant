from __future__ import annotations

from html import escape

from app.scanner.domain import ScannerResult, Setup, SetupRead


def format_ranking(setups: list[SetupRead], title: str) -> str:
    lines = [
        f"<b>{escape(title)}</b>",
        "Model confluence points, not success probability.",
    ]
    lines += [
        f"{i}. {escape(s.symbol)} — {s.direction} {s.score}/100 — {s.readiness}"
        for i, s in enumerate(
            sorted(setups, key=lambda s: (-s.score, s.symbol))[:10], 1
        )
    ]
    if not setups:
        lines.append(
            "No current qualifying setups. Run the scanner or wait for the next cycle."
        )
    return "\n".join(lines)


def format_setup(setup: Setup, result: ScannerResult, lifecycle: str = "ACTIVE") -> str:
    frame = result.frames["15m"]
    derivative = result.derivatives

    def zone_text(zones):
        return f"{zones[0].lower:f}–{zones[0].upper:f}" if zones else "unavailable"

    def number(value):
        return f"{value:.2f}" if value is not None else "unavailable"

    event = frame.micro.breaks[-1] if frame.micro.breaks else None
    terminal = lifecycle in ("INVALIDATED", "EXPIRED")
    return "\n".join(
        [
            f"<b>{escape(setup.state)} {setup.direction} WATCH · {escape(lifecycle)}</b>",
            f"<b>{escape(setup.symbol)}</b>",
            f"Score: {setup.score} / 100 model confluence points",
            "Not a probability of success.",
            f"4H trend: {result.frames['4h'].technical.alignment}",
            f"1H macro: {result.frames['1h'].macro.trend}",
            f"15m structure: {event.kind + ' ' + event.direction if event else frame.micro.trend}",
            f"EMA: {frame.technical.alignment}",
            f"RVOL: {number(frame.technical.rvol)} · ADX: {number(frame.technical.adx)} · RSI: {number(frame.technical.rsi)}",
            f"Funding: {derivative.funding_state}",
            f"OI change: {number(derivative.oi_change_pct)}% · {escape(derivative.interpretation)}",
            f"BTC/ETH context: {result.context.state}",
            f"Support: {zone_text(frame.supports)}",
            f"Resistance: {zone_text(frame.resistances)}",
            f"Invalidation: {setup.risk.invalidation if setup.risk.invalidation is not None else 'unavailable'}",
            f"R:R: {number(setup.risk.rr)} · {setup.risk.reason}",
            (
                "NEXT: Setup no longer active; wait for a new analysis."
                if terminal
                else f"NEXT: {setup.readiness} — {escape(setup.next_condition)}"
            ),
            f"Closed candle: {result.candle_closed_at.isoformat()}",
            "Educational signal only. No automatic trading.",
        ]
    )
