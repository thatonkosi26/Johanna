"""Deterministic market-analysis building blocks (Phase 2.1).

Everything here is pure: plain data in, plain data out, no clock reads, and no imports of
MetaTrader 5, broker or provider adapters, collectors or network code (test-enforced). Data
reaches this package only through ``johanna.live_market`` responses passed in by callers.

Implemented so far: ``snapshot`` (T2, the coordinated market-data snapshot). Indicators,
structure, sessions and eligibility windows belong to later, separately approved stages.
"""
