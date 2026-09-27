"""Early trend ignition / pattern formation / breakout-retest engine.

EARLY-WARNING layer only: it never changes a technical score, threshold, readiness,
risk plan or scanner alert rule. Closed-candle structure comes from the normal
analytics functions; realtime prices come from the fast watcher stream and are kept
strictly separate from the scanner's closed-candle data.
"""
