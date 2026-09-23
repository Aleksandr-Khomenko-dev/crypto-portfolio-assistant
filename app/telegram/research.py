"""On-demand research summaries. Outcomes are never pushed to Telegram."""

from __future__ import annotations

from html import escape

from app.research.schemas import BandStats, CalibrationReport


def _pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "n/a"


def _r(value: float | None) -> str:
    return f"{value:.2f}R" if value is not None else "n/a"


def format_band(band: BandStats) -> list[str]:
    warning = " ⚠️ small sample" if band.low_sample else ""
    lines = [f"<b>Score {band.band}</b>", f"Samples: {band.sample_count}{warning}"]
    lines += [
        f"{t.target} first: {_pct(t.observed_rate)} (resolved {t.resolved})"
        for t in band.targets
    ]
    lines += [
        f"Avg MFE: {_r(band.avg_mfe_r)} · Avg MAE: {_r(band.avg_mae_r)}",
        f"Ambiguous: {band.ambiguous_count} · Expired: {band.expired_count}",
    ]
    return lines


def format_calibration(report: CalibrationReport, title: str) -> str:
    lines = [
        f"<b>{escape(title)}</b>",
        "Historical scanner statistics — observed rates of setup outcomes.",
        "Not trades, not a probability of future results.",
        f"Still tracking: {report.tracking_count}",
    ]
    if not any(group.sample_count for group in report.groups):
        lines.append("\nNo completed setup outcomes yet. Keep the scanner collecting.")
        return "\n".join(lines)
    for group in report.groups:
        if group.key:
            label = " · ".join(escape(v) for v in group.key.values())
            lines.append(f"\n<b>{label}</b> — samples: {group.sample_count}")
        for band in group.bands:
            if band.sample_count:
                lines += ["", *format_band(band)]
    lines.append(
        f"\nBands under {report.min_samples} samples are too small for conclusions."
    )
    return "\n".join(lines)
