"""Phase 5 trading-journal store (P5.1). See ``docs/phase-5-architecture.md`` §3, §5, §9.

A separate SQLite database, ``{data_dir}/trading_journal.sqlite3``, with its HMAC key
``{data_dir}/trading_journal.key``. Unlike ``ApplicationStore`` it **fails closed**: a corrupt or
unexpected database is refused, never quarantined or recreated, and a key is never regenerated.

Only these names are ever touched in ``data_dir``: the database (and SQLite's own ``-wal`` /
``-shm`` sidecars), the key, published backup-pair directories ``trading_journal.backup-*`` and
temporary backup directories ``.trading_journal.backup-tmp-*``. Nothing else is listed or read.

Audit chain (§9): every audit row is hashed together with the previous row's hash. The chain is
**tamper-evident, not tamper-proof**: there is no independent external anchor for the chain
head, so someone able to rewrite the database file can rewrite rows *and* recompute the chain.
It detects accidental or unauthorised edits of individual rows only.

Canonical audit hash (exact specification):

- Hashed fields, in this order: ``seq`` (integer), ``recorded_utc`` (UTC ISO-8601 with
  microseconds and ``Z``, e.g. ``2026-11-17T10:31:46.024241Z``), ``actor``, ``action``,
  ``table_name``, ``record_id`` (text or null), ``old_values`` and ``new_values`` (each a
  canonical JSON string or null), ``prev_hash`` (64 lowercase hex characters).
- Canonical JSON for values: ``json.dumps(value, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False, allow_nan=False)``.
- Row bytes: the nine fields as a JSON array, serialised with ``separators=(",", ":")``,
  ``ensure_ascii=False`` and ``allow_nan=False``, encoded as UTF-8.
- ``row_hash = SHA-256(row bytes)`` as lowercase hex. The genesis ``prev_hash`` is 64 zeros;
  every later row's ``prev_hash`` is the previous row's ``row_hash``.
- Verification walks rows by ``seq`` from 1: sequence numbers must be contiguous (detects
  deletion and reordering), each ``prev_hash`` must equal the previous ``row_hash`` and each
  ``row_hash`` must equal the recomputed hash (detects modification).

Insertion and verification use the same function (``audit_row_hash``) over the same stored field
values. Limitations: verification hashes the *stored* ``old_values``/``new_values`` text and does
not re-canonicalise it, so any byte change is detected, including a semantically equivalent
re-serialisation; non-finite numbers (NaN, ±Infinity) are rejected at insertion; floats are
serialised by Python's shortest round-trip ``repr``, so the chain is reproducible by any
verifier using the same rule; the chain cannot detect a full rewrite with recomputed hashes.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("johanna.journal")

DB_NAME = "trading_journal.sqlite3"
KEY_NAME = "trading_journal.key"
PAIR_PREFIX = "trading_journal.backup-"
TMP_PREFIX = ".trading_journal.backup-tmp-"
PAIR_MANIFEST = "pair.json"
SIDECARS = ("-wal", "-shm", "-journal")
KEY_BYTES = 32
KEY_CHECK_MESSAGE = b"johanna-journal-key-check"
GENESIS_HASH = "0" * 64
DAILY_RETENTION = 7
_PAIR_NAME = re.compile(r"trading_journal\.backup-(daily|premigration-v\d+)-(\d{8}T\d{12}Z)")

# --------------------------------------------------------------------------- schema
# Forward-only migrations: (target user_version, SQL). Never edit a released migration.
SCHEMA_V1 = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE decision (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    broker_symbol TEXT,
    timeframe TEXT NOT NULL,
    as_of_utc TEXT,
    analysis_outcome TEXT NOT NULL
        CHECK (analysis_outcome IN ('TRADE_CANDIDATE', 'MARKET_CONDITION', 'NO_TRADE')),
    t6_rule TEXT,
    market_direction TEXT NOT NULL CHECK (market_direction IN ('BUY', 'SELL', 'NONE')),
    t6_evidence TEXT NOT NULL,
    analysis_fingerprint TEXT NOT NULL,
    snapshot_id TEXT,
    evaluated_at_utc TEXT,
    previous_decision_id TEXT REFERENCES decision(id),
    capture_source TEXT NOT NULL CHECK (capture_source IN ('analysis_request', 'risk_request')),
    recorded_utc TEXT NOT NULL,
    UNIQUE (symbol, timeframe, analysis_fingerprint, previous_decision_id)
);
CREATE INDEX decision_key ON decision (symbol, timeframe);
CREATE TABLE decision_observation (
    decision_id TEXT PRIMARY KEY REFERENCES decision(id),
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    observations INTEGER NOT NULL CHECK (observations >= 1)
);
CREATE TABLE risk_assessment (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision(id),
    risk_status TEXT NOT NULL
        CHECK (risk_status IN ('APPROVED', 'REJECTED', 'UNVERIFIED', 'NOT_EVALUATED')),
    phase4_status_raw TEXT,
    risk_evidence TEXT NOT NULL,
    risk_fingerprint TEXT NOT NULL,
    evaluated_at_utc TEXT,
    previous_assessment_id TEXT REFERENCES risk_assessment(id),
    recorded_utc TEXT NOT NULL,
    UNIQUE (decision_id, risk_fingerprint, previous_assessment_id)
);
CREATE INDEX risk_by_decision ON risk_assessment (decision_id);
CREATE TABLE risk_assessment_observation (
    assessment_id TEXT PRIMARY KEY REFERENCES risk_assessment(id),
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    observations INTEGER NOT NULL CHECK (observations >= 1)
);
CREATE TABLE event (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    subject_ref TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    severity TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_utc TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    recovered INTEGER NOT NULL DEFAULT 0 CHECK (recovered IN (0, 1))
);
CREATE TABLE audit_log (
    seq INTEGER PRIMARY KEY,
    recorded_utc TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id TEXT,
    old_values TEXT,
    new_values TEXT,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);

-- Immutable evidence, append-only events, audit and meta: UPDATE and DELETE abort.
CREATE TRIGGER meta_no_update BEFORE UPDATE ON meta
BEGIN SELECT RAISE(ABORT, 'meta is insert-only'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta
BEGIN SELECT RAISE(ABORT, 'meta is insert-only'); END;
CREATE TRIGGER decision_no_update BEFORE UPDATE ON decision
BEGIN SELECT RAISE(ABORT, 'decision evidence is immutable'); END;
CREATE TRIGGER decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'decision evidence is immutable'); END;
CREATE TRIGGER risk_no_update BEFORE UPDATE ON risk_assessment
BEGIN SELECT RAISE(ABORT, 'risk assessment evidence is immutable'); END;
CREATE TRIGGER risk_no_delete BEFORE DELETE ON risk_assessment
BEGIN SELECT RAISE(ABORT, 'risk assessment evidence is immutable'); END;
CREATE TRIGGER event_no_update BEFORE UPDATE ON event
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER event_no_delete BEFORE DELETE ON event
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

-- Observation metadata: only last_seen_utc and a growing observation count may change.
CREATE TRIGGER decision_obs_guard BEFORE UPDATE ON decision_observation
WHEN NEW.decision_id IS NOT OLD.decision_id
  OR NEW.first_seen_utc IS NOT OLD.first_seen_utc
  OR NEW.observations < OLD.observations
  OR NEW.last_seen_utc < OLD.last_seen_utc
BEGIN SELECT RAISE(ABORT, 'observation metadata may only advance'); END;
CREATE TRIGGER decision_obs_no_delete BEFORE DELETE ON decision_observation
BEGIN SELECT RAISE(ABORT, 'observation metadata cannot be deleted'); END;
CREATE TRIGGER risk_obs_guard BEFORE UPDATE ON risk_assessment_observation
WHEN NEW.assessment_id IS NOT OLD.assessment_id
  OR NEW.first_seen_utc IS NOT OLD.first_seen_utc
  OR NEW.observations < OLD.observations
  OR NEW.last_seen_utc < OLD.last_seen_utc
BEGIN SELECT RAISE(ABORT, 'observation metadata may only advance'); END;
CREATE TRIGGER risk_obs_no_delete BEFORE DELETE ON risk_assessment_observation
BEGIN SELECT RAISE(ABORT, 'observation metadata cannot be deleted'); END;
"""
# P5.2 manual trade logbook (docs/phase-5-architecture.md §3.4; approved P5.2 design).
SCHEMA_V2 = """
CREATE TABLE trade (
    id TEXT PRIMARY KEY,
    canonical_symbol TEXT NOT NULL CHECK (canonical_symbol IN ('EURUSD', 'GBPUSD', 'USDJPY')),
    broker_symbol TEXT NOT NULL CHECK (broker_symbol IN ('EURUSDc', 'GBPUSDc', 'USDJPYc')),
    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    execution_source TEXT NOT NULL CHECK (execution_source IN ('manual', 'automated')),
    strategy_tag TEXT,
    instrument_spec TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    voided_utc TEXT,
    voided_reason TEXT,
    CHECK ((voided_utc IS NULL) = (voided_reason IS NULL))
);
CREATE TABLE trade_fill (
    id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trade(id),
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'exit')),
    time_utc TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    volume REAL NOT NULL CHECK (volume > 0),
    commission REAL,
    commission_source TEXT NOT NULL CHECK (commission_source IN ('user_entered', 'unknown')),
    swap REAL,
    swap_source TEXT NOT NULL CHECK (swap_source IN ('user_entered', 'unknown')),
    fee REAL,
    fee_source TEXT NOT NULL CHECK (fee_source IN ('user_entered', 'unknown')),
    realised_pnl_user REAL,
    realised_pnl_user_source TEXT CHECK (realised_pnl_user_source IN ('user_entered', 'unknown')),
    execution_source TEXT NOT NULL CHECK (execution_source IN ('manual', 'automated')),
    requested_price REAL,
    broker_deal_ref TEXT,
    execution_attempt_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('RECORDED', 'VOIDED')),
    voided_reason TEXT,
    voided_utc TEXT,
    corrects_fill_id TEXT REFERENCES trade_fill(id),
    created_utc TEXT NOT NULL,
    CHECK ((commission IS NULL) = (commission_source = 'unknown')),
    CHECK ((swap IS NULL) = (swap_source = 'unknown')),
    CHECK ((fee IS NULL) = (fee_source = 'unknown')),
    CHECK (kind = 'exit' OR (realised_pnl_user IS NULL AND realised_pnl_user_source IS NULL)),
    CHECK (kind = 'entry' OR realised_pnl_user_source IS NOT NULL),
    CHECK (realised_pnl_user_source IS NULL
           OR (realised_pnl_user IS NULL) = (realised_pnl_user_source = 'unknown')),
    CHECK ((status = 'VOIDED') = (voided_reason IS NOT NULL AND voided_utc IS NOT NULL))
);
CREATE INDEX trade_fill_by_trade ON trade_fill (trade_id);
CREATE TABLE trade_protection (
    id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trade(id),
    sl REAL CHECK (sl IS NULL OR sl > 0),
    tp REAL CHECK (tp IS NULL OR tp > 0),
    effective_utc TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'automated')),
    reason TEXT,
    recorded_utc TEXT NOT NULL
);
CREATE INDEX trade_protection_by_trade ON trade_protection (trade_id);
CREATE TABLE trade_note (
    id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trade(id),
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 4000),
    created_utc TEXT NOT NULL
);
CREATE INDEX trade_note_by_trade ON trade_note (trade_id);
CREATE TABLE trade_decision_link (
    id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trade(id),
    decision_id TEXT NOT NULL REFERENCES decision(id),
    risk_assessment_id TEXT REFERENCES risk_assessment(id),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'VOIDED')),
    voided_reason TEXT,
    voided_utc TEXT,
    created_utc TEXT NOT NULL,
    CHECK ((status = 'VOIDED') = (voided_reason IS NOT NULL AND voided_utc IS NOT NULL))
);
CREATE UNIQUE INDEX trade_decision_link_active
    ON trade_decision_link (trade_id, decision_id) WHERE status = 'ACTIVE';
CREATE INDEX trade_decision_link_by_decision ON trade_decision_link (decision_id);
CREATE TABLE write_request (
    request_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response TEXT NOT NULL,
    recorded_utc TEXT NOT NULL
);

-- Trades: the only permitted change is a single, one-way void (reason and time).
CREATE TRIGGER trade_void_only BEFORE UPDATE ON trade
WHEN OLD.voided_utc IS NOT NULL
  OR NEW.voided_utc IS NULL
  OR NEW.id IS NOT OLD.id
  OR NEW.canonical_symbol IS NOT OLD.canonical_symbol
  OR NEW.broker_symbol IS NOT OLD.broker_symbol
  OR NEW.direction IS NOT OLD.direction
  OR NEW.execution_source IS NOT OLD.execution_source
  OR NEW.strategy_tag IS NOT OLD.strategy_tag
  OR NEW.instrument_spec IS NOT OLD.instrument_spec
  OR NEW.created_utc IS NOT OLD.created_utc
BEGIN SELECT RAISE(ABORT, 'trades are void-only'); END;
CREATE TRIGGER trade_no_delete BEFORE DELETE ON trade
BEGIN SELECT RAISE(ABORT, 'trades are void-only'); END;

-- Fills: void-only evidence (one-way RECORDED -> VOIDED; every other column unchanged).
CREATE TRIGGER trade_fill_void_only BEFORE UPDATE ON trade_fill
WHEN OLD.status <> 'RECORDED' OR NEW.status <> 'VOIDED'
  OR NEW.id IS NOT OLD.id OR NEW.trade_id IS NOT OLD.trade_id OR NEW.kind IS NOT OLD.kind
  OR NEW.time_utc IS NOT OLD.time_utc OR NEW.price IS NOT OLD.price
  OR NEW.volume IS NOT OLD.volume
  OR NEW.commission IS NOT OLD.commission OR NEW.commission_source IS NOT OLD.commission_source
  OR NEW.swap IS NOT OLD.swap OR NEW.swap_source IS NOT OLD.swap_source
  OR NEW.fee IS NOT OLD.fee OR NEW.fee_source IS NOT OLD.fee_source
  OR NEW.realised_pnl_user IS NOT OLD.realised_pnl_user
  OR NEW.realised_pnl_user_source IS NOT OLD.realised_pnl_user_source
  OR NEW.execution_source IS NOT OLD.execution_source
  OR NEW.requested_price IS NOT OLD.requested_price
  OR NEW.broker_deal_ref IS NOT OLD.broker_deal_ref
  OR NEW.execution_attempt_id IS NOT OLD.execution_attempt_id
  OR NEW.corrects_fill_id IS NOT OLD.corrects_fill_id
  OR NEW.created_utc IS NOT OLD.created_utc
BEGIN SELECT RAISE(ABORT, 'fills are void-only evidence'); END;
CREATE TRIGGER trade_fill_no_delete BEFORE DELETE ON trade_fill
BEGIN SELECT RAISE(ABORT, 'fills are void-only evidence'); END;

-- Links: void-only.
CREATE TRIGGER trade_link_void_only BEFORE UPDATE ON trade_decision_link
WHEN OLD.status <> 'ACTIVE' OR NEW.status <> 'VOIDED'
  OR NEW.id IS NOT OLD.id OR NEW.trade_id IS NOT OLD.trade_id
  OR NEW.decision_id IS NOT OLD.decision_id
  OR NEW.risk_assessment_id IS NOT OLD.risk_assessment_id
  OR NEW.created_utc IS NOT OLD.created_utc
BEGIN SELECT RAISE(ABORT, 'links are void-only'); END;
CREATE TRIGGER trade_link_no_delete BEFORE DELETE ON trade_decision_link
BEGIN SELECT RAISE(ABORT, 'links are void-only'); END;

-- Protection history, notes and idempotency records: append-only.
CREATE TRIGGER trade_protection_no_update BEFORE UPDATE ON trade_protection
BEGIN SELECT RAISE(ABORT, 'protection history is append-only'); END;
CREATE TRIGGER trade_protection_no_delete BEFORE DELETE ON trade_protection
BEGIN SELECT RAISE(ABORT, 'protection history is append-only'); END;
CREATE TRIGGER trade_note_no_update BEFORE UPDATE ON trade_note
BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
CREATE TRIGGER trade_note_no_delete BEFORE DELETE ON trade_note
BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
CREATE TRIGGER write_request_no_update BEFORE UPDATE ON write_request
BEGIN SELECT RAISE(ABORT, 'write requests are append-only'); END;
CREATE TRIGGER write_request_no_delete BEFORE DELETE ON write_request
BEGIN SELECT RAISE(ABORT, 'write requests are append-only'); END;
"""
# P6.1 shadow evaluation and signal-frequency diagnostics. Additive, except that the decision
# table is rebuilt (rows copied verbatim) so its capture_source CHECK can admit
# 'scheduled_evaluation'; its index and immutability triggers are recreated unchanged.
SCHEMA_V3 = """
CREATE TABLE decision_v3 (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    broker_symbol TEXT,
    timeframe TEXT NOT NULL,
    as_of_utc TEXT,
    analysis_outcome TEXT NOT NULL
        CHECK (analysis_outcome IN ('TRADE_CANDIDATE', 'MARKET_CONDITION', 'NO_TRADE')),
    t6_rule TEXT,
    market_direction TEXT NOT NULL CHECK (market_direction IN ('BUY', 'SELL', 'NONE')),
    t6_evidence TEXT NOT NULL,
    analysis_fingerprint TEXT NOT NULL,
    snapshot_id TEXT,
    evaluated_at_utc TEXT,
    previous_decision_id TEXT REFERENCES decision(id),
    capture_source TEXT NOT NULL
        CHECK (capture_source IN ('analysis_request', 'risk_request', 'scheduled_evaluation')),
    recorded_utc TEXT NOT NULL,
    UNIQUE (symbol, timeframe, analysis_fingerprint, previous_decision_id)
);
INSERT INTO decision_v3 SELECT * FROM decision;
DROP TABLE decision;
ALTER TABLE decision_v3 RENAME TO decision;
CREATE INDEX decision_key ON decision (symbol, timeframe);
CREATE TRIGGER decision_no_update BEFORE UPDATE ON decision
BEGIN SELECT RAISE(ABORT, 'decision evidence is immutable'); END;
CREATE TRIGGER decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'decision evidence is immutable'); END;

-- Scheduler enable/disable history: append-only; the latest row is the current state and no
-- row means disabled (first installation and migrations never enable the scheduler).
CREATE TABLE shadow_scheduler_setting (
    id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    changed_utc TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (actor = 'user')
);
CREATE TRIGGER shadow_setting_no_update BEFORE UPDATE ON shadow_scheduler_setting
BEGIN SELECT RAISE(ABORT, 'scheduler settings are append-only'); END;
CREATE TRIGGER shadow_setting_no_delete BEFORE DELETE ON shadow_scheduler_setting
BEGIN SELECT RAISE(ABORT, 'scheduler settings are append-only'); END;

-- One row per scheduled evaluation window (dedup_key unique).
CREATE TABLE evaluation_cycle (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL CHECK (symbol IN ('EURUSD', 'GBPUSD', 'USDJPY')),
    broker_symbol TEXT,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('H1', 'H4', 'D1')),
    window_close_utc TEXT NOT NULL,
    analysis_version INTEGER NOT NULL,
    decision_version INTEGER NOT NULL,
    risk_version INTEGER NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'SKIPPED', 'MISSED', 'FAILED')),
    reason_code TEXT,
    reason_detail TEXT,
    data_quality TEXT,
    t6_outcome TEXT
        CHECK (t6_outcome IS NULL
               OR t6_outcome IN ('TRADE_CANDIDATE', 'MARKET_CONDITION', 'NO_TRADE')),
    t6_rule TEXT,
    market_direction TEXT,
    risk_status TEXT
        CHECK (risk_status IS NULL
               OR risk_status IN ('APPROVED', 'REJECTED', 'UNVERIFIED', 'NOT_EVALUATED')),
    decision_id TEXT REFERENCES decision(id),
    risk_assessment_id TEXT REFERENCES risk_assessment(id),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    contention_deferrals INTEGER NOT NULL DEFAULT 0 CHECK (contention_deferrals >= 0),
    owner_run TEXT,
    next_attempt_utc TEXT,
    deadline_utc TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    started_utc TEXT,
    completed_utc TEXT,
    CHECK (status IN ('PENDING', 'RUNNING') OR completed_utc IS NOT NULL),
    CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED') OR reason_code IS NOT NULL),
    CHECK (status <> 'COMPLETED' OR t6_outcome IS NOT NULL)
);
CREATE INDEX evaluation_cycle_by_key ON evaluation_cycle (symbol, timeframe, window_close_utc);
CREATE INDEX evaluation_cycle_by_status ON evaluation_cycle (status);
CREATE TRIGGER evaluation_cycle_insert_state BEFORE INSERT ON evaluation_cycle
WHEN NEW.status NOT IN ('PENDING', 'SKIPPED', 'MISSED')
BEGIN SELECT RAISE(ABORT, 'evaluation cycles start PENDING, SKIPPED or MISSED'); END;
-- Allowed transitions: PENDING -> PENDING | RUNNING | SKIPPED | MISSED;
-- RUNNING -> PENDING (retry) | COMPLETED | SKIPPED | FAILED. Terminal rows never change and
-- the identity of a window never changes.
CREATE TRIGGER evaluation_cycle_transitions BEFORE UPDATE ON evaluation_cycle
WHEN OLD.status NOT IN ('PENDING', 'RUNNING')
  OR (OLD.status = 'PENDING' AND NEW.status NOT IN ('PENDING', 'RUNNING', 'SKIPPED', 'MISSED'))
  OR (OLD.status = 'RUNNING'
      AND NEW.status NOT IN ('PENDING', 'COMPLETED', 'SKIPPED', 'FAILED'))
  OR NEW.id IS NOT OLD.id OR NEW.dedup_key IS NOT OLD.dedup_key
  OR NEW.symbol IS NOT OLD.symbol OR NEW.timeframe IS NOT OLD.timeframe
  OR NEW.window_close_utc IS NOT OLD.window_close_utc
  OR NEW.analysis_version IS NOT OLD.analysis_version
  OR NEW.decision_version IS NOT OLD.decision_version
  OR NEW.risk_version IS NOT OLD.risk_version
  OR NEW.deadline_utc IS NOT OLD.deadline_utc OR NEW.created_utc IS NOT OLD.created_utc
  OR NEW.attempts < OLD.attempts OR NEW.contention_deferrals < OLD.contention_deferrals
BEGIN SELECT RAISE(ABORT, 'invalid evaluation cycle transition'); END;
CREATE TRIGGER evaluation_cycle_no_delete BEFORE DELETE ON evaluation_cycle
BEGIN SELECT RAISE(ABORT, 'evaluation cycles cannot be deleted'); END;

-- Gate outcomes of one evaluation: append-only, written with the terminal transition.
CREATE TABLE gate_outcome (
    id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL REFERENCES evaluation_cycle(id),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    stage TEXT NOT NULL
        CHECK (stage IN ('schedule', 'session', 'contention', 'data', 'closure', 'analysis',
                         'strategy', 'risk')),
    gate TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS', 'FAIL', 'BLOCKED', 'SKIPPED')),
    reason_code TEXT,
    detail TEXT CHECK (detail IS NULL OR length(detail) <= 500),
    recorded_utc TEXT NOT NULL,
    UNIQUE (evaluation_id, seq)
);
CREATE INDEX gate_outcome_by_evaluation ON gate_outcome (evaluation_id);
CREATE INDEX gate_outcome_by_gate ON gate_outcome (stage, gate, result);
CREATE TRIGGER gate_outcome_no_update BEFORE UPDATE ON gate_outcome
BEGIN SELECT RAISE(ABORT, 'gate outcomes are append-only'); END;
CREATE TRIGGER gate_outcome_no_delete BEFORE DELETE ON gate_outcome
BEGIN SELECT RAISE(ABORT, 'gate outcomes are append-only'); END;
"""
# P6.2a shadow trade intents. Purely additive: one new append-only table, its indexes and
# triggers; no existing table is rebuilt or changed. A shadow intent is a HYPOTHETICAL record of
# a scheduled candidate's constructed trade and its risk eligibility -- never an order.
SCHEMA_V4 = """
CREATE TABLE shadow_intent (
    id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL UNIQUE REFERENCES evaluation_cycle(id),
    decision_id TEXT NOT NULL REFERENCES decision(id),
    risk_assessment_id TEXT NOT NULL REFERENCES risk_assessment(id),
    kind TEXT NOT NULL CHECK (kind = 'SHADOW_HYPOTHETICAL'),
    symbol TEXT NOT NULL CHECK (symbol IN ('EURUSD', 'GBPUSD', 'USDJPY')),
    broker_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('H1', 'H4', 'D1')),
    window_close_utc TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    eligibility TEXT NOT NULL CHECK (eligibility IN ('RISK_APPROVED', 'REJECTED', 'UNVERIFIED')),
    entry_price REAL NOT NULL CHECK (entry_price > 0),
    entry_side TEXT NOT NULL CHECK (entry_side IN ('ask', 'bid')),
    entry_reference_utc TEXT NOT NULL,
    valid_until_utc TEXT,
    stop_loss REAL NOT NULL CHECK (stop_loss > 0),
    take_profit REAL NOT NULL CHECK (take_profit > 0),
    target_method TEXT NOT NULL CHECK (target_method = 'structural_tc6'),
    volume REAL CHECK (volume IS NULL OR volume > 0),
    gross_reward_risk REAL NOT NULL CHECK (gross_reward_risk > 0),
    net_reward_risk REAL,
    risk_amount_usc REAL CHECK (risk_amount_usc IS NULL OR risk_amount_usc >= 0),
    risk_percent_equity REAL CHECK (risk_percent_equity IS NULL OR risk_percent_equity >= 0),
    margin_estimate_usc REAL CHECK (margin_estimate_usc IS NULL OR margin_estimate_usc >= 0),
    cost_status TEXT NOT NULL CHECK (cost_status IN ('established', 'unverified')),
    margin_status TEXT NOT NULL
        CHECK (margin_status IN ('passed', 'failed', 'unverified', 'not_evaluated')),
    account_evidence TEXT NOT NULL,
    cost_evidence TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    risk_engine_version INTEGER NOT NULL CHECK (risk_engine_version >= 2),
    cost_terms_version TEXT NOT NULL,
    reasons TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    -- a volume exists exactly when the intent is risk-approved (never invented otherwise)
    CHECK ((eligibility = 'RISK_APPROVED') = (volume IS NOT NULL)),
    CHECK (eligibility <> 'RISK_APPROVED' OR (risk_amount_usc IS NOT NULL
           AND risk_percent_equity IS NOT NULL AND net_reward_risk IS NOT NULL
           AND margin_estimate_usc IS NOT NULL AND cost_status = 'established'
           AND margin_status = 'passed' AND cost_terms_version <> 'unavailable')),
    CHECK ((direction = 'BUY' AND entry_side = 'ask' AND stop_loss < entry_price
            AND take_profit > entry_price)
        OR (direction = 'SELL' AND entry_side = 'bid' AND stop_loss > entry_price
            AND take_profit < entry_price))
);
CREATE INDEX shadow_intent_by_key ON shadow_intent (symbol, timeframe, window_close_utc);
CREATE INDEX shadow_intent_by_eligibility ON shadow_intent (eligibility);
-- Only a completed candidate evaluation of the scheduler may carry an intent, with the same
-- decision, risk assessment, window and (mapped) risk status as that evaluation.
CREATE TRIGGER shadow_intent_links BEFORE INSERT ON shadow_intent
WHEN NOT EXISTS (
    SELECT 1 FROM evaluation_cycle c
    WHERE c.id = NEW.evaluation_id AND c.status = 'COMPLETED'
      AND c.t6_outcome = 'TRADE_CANDIDATE'
      AND c.decision_id = NEW.decision_id AND c.risk_assessment_id = NEW.risk_assessment_id
      AND c.symbol = NEW.symbol AND c.timeframe = NEW.timeframe
      AND c.window_close_utc = NEW.window_close_utc
      AND c.risk_status = (CASE NEW.eligibility WHEN 'RISK_APPROVED' THEN 'APPROVED'
                           ELSE NEW.eligibility END))
BEGIN SELECT RAISE(ABORT, 'shadow intent does not match a completed candidate evaluation'); END;
CREATE TRIGGER shadow_intent_no_update BEFORE UPDATE ON shadow_intent
BEGIN SELECT RAISE(ABORT, 'shadow intents are append-only'); END;
CREATE TRIGGER shadow_intent_no_delete BEFORE DELETE ON shadow_intent
BEGIN SELECT RAISE(ABORT, 'shadow intents are append-only'); END;
"""
# P6.2b.2 / P6.3 / P6.4, corrected in Phase 1 (H-1 .. M-5). Additive only: the cost-policy
# registry, read-only broker observations and reconciliation results, and local
# notifications. Registered in Phase 2 with the user's approval; it is applied to a journal
# only when the backend next starts (verified pre-migration backup first).
SCHEMA_V5 = """
CREATE TABLE cost_policy_record (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('commission', 'slippage', 'holding')),
    timeframe TEXT CHECK (timeframe IS NULL OR timeframe IN ('H1', 'H4', 'D1')),
    value REAL NOT NULL CHECK (value >= 0),
    currency TEXT CHECK (currency IS NULL OR currency IN ('USC', 'USD')),
    unit TEXT NOT NULL CHECK (unit IN ('amount_per_lot', 'points', 'pips', 'hours')),
    basis TEXT NOT NULL CHECK (basis IN ('per_side', 'round_trip', 'per_position')),
    instruments TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('UNVERIFIED', 'PROVISIONAL', 'PROPOSED')),
    record_json TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL UNIQUE,
    -- M-1: one broker-terms fingerprint per covered instrument (JSON object), never one
    -- fingerprint for several instruments. M-5: units per lot for per-lot commission.
    broker_terms TEXT CHECK (broker_terms IS NULL
        OR (json_valid(broker_terms) AND json_type(broker_terms) = 'object')),
    lot_units REAL,
    supersedes_id TEXT REFERENCES cost_policy_record(id),
    actor TEXT NOT NULL CHECK (actor IN ('user', 'johanna')),
    recorded_utc TEXT NOT NULL,
    CHECK ((kind = 'holding') = (timeframe IS NOT NULL)),
    CHECK ((kind = 'commission') = (lot_units IS NOT NULL)),
    CHECK (lot_units IS NULL OR lot_units > 0),
    CHECK (kind <> 'holding' OR (unit = 'hours' AND basis = 'per_position'
           AND currency IS NULL AND status = 'PROPOSED' AND value > 0
           AND broker_terms IS NULL)),
    CHECK (kind <> 'commission' OR (unit = 'amount_per_lot' AND currency IS NOT NULL
           AND basis IN ('per_side', 'round_trip') AND status IN ('UNVERIFIED', 'PROVISIONAL'))),
    CHECK (kind <> 'slippage' OR (unit IN ('points', 'pips') AND currency IS NULL
           AND basis IN ('per_side', 'round_trip') AND status IN ('UNVERIFIED', 'PROVISIONAL')))
);
CREATE INDEX cost_policy_record_by_kind ON cost_policy_record (kind, timeframe);
CREATE TRIGGER cost_policy_record_no_update BEFORE UPDATE ON cost_policy_record
BEGIN SELECT RAISE(ABORT, 'cost policy records are append-only'); END;
CREATE TRIGGER cost_policy_record_no_delete BEFORE DELETE ON cost_policy_record
BEGIN SELECT RAISE(ABORT, 'cost policy records are append-only'); END;
-- Decisions: only the user approves or rejects; Johanna may only invalidate (e.g. changed
-- broker terms). Nothing follows a rejection or an invalidation.
CREATE TABLE cost_policy_decision (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES cost_policy_record(id),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED', 'INVALIDATED')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    confirmed_value TEXT,
    actor TEXT NOT NULL CHECK (actor IN ('user', 'johanna')),
    decided_utc TEXT NOT NULL,
    CHECK (decision = 'INVALIDATED' OR actor = 'user'),
    CHECK (decision <> 'APPROVED' OR confirmed_value IS NOT NULL)
);
CREATE INDEX cost_policy_decision_by_record ON cost_policy_decision (record_id);
CREATE TRIGGER cost_policy_decision_terminal BEFORE INSERT ON cost_policy_decision
WHEN EXISTS (SELECT 1 FROM cost_policy_decision d WHERE d.record_id = NEW.record_id
             AND d.decision IN ('REJECTED', 'INVALIDATED'))
BEGIN SELECT RAISE(ABORT, 'a rejected or invalidated record takes no further decision'); END;
CREATE TRIGGER cost_policy_decision_no_update BEFORE UPDATE ON cost_policy_decision
BEGIN SELECT RAISE(ABORT, 'cost policy decisions are append-only'); END;
CREATE TRIGGER cost_policy_decision_no_delete BEFORE DELETE ON cost_policy_decision
BEGIN SELECT RAISE(ABORT, 'cost policy decisions are append-only'); END;

-- Read-only broker observations (HMAC references and allowlisted fields only).
CREATE TABLE broker_observation (
    id TEXT PRIMARY KEY,
    observed_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('LIVE', 'UNAVAILABLE')),
    reason TEXT,
    coverage_from_utc TEXT,
    coverage_to_utc TEXT,
    positions INTEGER CHECK (positions IS NULL OR positions >= 0),
    orders INTEGER CHECK (orders IS NULL OR orders >= 0),
    deals INTEGER CHECK (deals IS NULL OR deals >= 0),
    payload TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    CHECK (status <> 'LIVE' OR (coverage_from_utc IS NOT NULL AND coverage_to_utc IS NOT NULL
           AND positions IS NOT NULL AND orders IS NOT NULL AND deals IS NOT NULL)),
    CHECK (status <> 'UNAVAILABLE' OR reason IS NOT NULL)
);
CREATE INDEX broker_observation_by_time ON broker_observation (observed_utc);
CREATE TRIGGER broker_observation_no_update BEFORE UPDATE ON broker_observation
BEGIN SELECT RAISE(ABORT, 'broker observations are append-only'); END;
CREATE TRIGGER broker_observation_no_delete BEFORE DELETE ON broker_observation
BEGIN SELECT RAISE(ABORT, 'broker observations are append-only'); END;
CREATE TABLE reconciliation_result (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES broker_observation(id),
    subject TEXT NOT NULL CHECK (subject IN ('trade', 'broker_position')),
    -- M-3: a non-null logical key (the trade id, or the position reference) so that one
    -- observation can never hold two results for the same subject (NULLs are never equal).
    subject_key TEXT NOT NULL,
    trade_id TEXT REFERENCES trade(id),
    position_ref TEXT,
    state TEXT NOT NULL CHECK (state IN ('CONFIRMED_OPEN', 'CONFIRMED_CLOSED', 'PENDING',
        'AMBIGUOUS', 'CANDIDATE_MATCH', 'UNMATCHED', 'UNAVAILABLE', 'UNRECORDED_POSITION')),
    basis TEXT NOT NULL CHECK (basis IN ('broker_confirmed', 'inferred', 'unavailable')),
    detail TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    CHECK ((subject = 'trade') = (trade_id IS NOT NULL)),
    CHECK (subject <> 'broker_position' OR (position_ref IS NOT NULL
           AND state = 'UNRECORDED_POSITION')),
    CHECK (state NOT IN ('CONFIRMED_OPEN', 'CONFIRMED_CLOSED') OR basis = 'broker_confirmed'),
    CHECK ((subject = 'trade' AND subject_key = trade_id)
        OR (subject = 'broker_position' AND subject_key = position_ref)),
    UNIQUE (observation_id, subject, subject_key)
);
CREATE INDEX reconciliation_result_by_trade ON reconciliation_result (trade_id);
CREATE TRIGGER reconciliation_result_no_update BEFORE UPDATE ON reconciliation_result
BEGIN SELECT RAISE(ABORT, 'reconciliation results are append-only'); END;
CREATE TRIGGER reconciliation_result_no_delete BEFORE DELETE ON reconciliation_result
BEGIN SELECT RAISE(ABORT, 'reconciliation results are append-only'); END;

-- Local notifications: immutable content, append-only lifecycle and delivery attempts.
CREATE TABLE notification (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL
        CHECK (category IN ('information', 'trade_candidate', 'risk_rejection', 'broker_event')),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 1000),
    subject_ref TEXT,
    created_utc TEXT NOT NULL
);
CREATE TRIGGER notification_no_update BEFORE UPDATE ON notification
BEGIN SELECT RAISE(ABORT, 'notifications are immutable'); END;
CREATE TRIGGER notification_no_delete BEFORE DELETE ON notification
BEGIN SELECT RAISE(ABORT, 'notifications are immutable'); END;
CREATE TABLE notification_state (
    id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES notification(id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'available', 'presented', 'acknowledged')),
    actor TEXT NOT NULL CHECK (actor IN ('johanna', 'user')),
    changed_utc TEXT NOT NULL,
    UNIQUE (notification_id, state),
    CHECK (state <> 'acknowledged' OR actor = 'user')
);
-- pending -> available -> presented -> acknowledged, each once and in order.
CREATE TRIGGER notification_state_order BEFORE INSERT ON notification_state
WHEN NOT (
    (NEW.state = 'pending' AND NOT EXISTS (SELECT 1 FROM notification_state s
        WHERE s.notification_id = NEW.notification_id))
    OR (NEW.state = 'available' AND EXISTS (SELECT 1 FROM notification_state s
        WHERE s.notification_id = NEW.notification_id AND s.state = 'pending'))
    OR (NEW.state = 'presented' AND EXISTS (SELECT 1 FROM notification_state s
        WHERE s.notification_id = NEW.notification_id AND s.state = 'available'))
    OR (NEW.state = 'acknowledged' AND EXISTS (SELECT 1 FROM notification_state s
        WHERE s.notification_id = NEW.notification_id AND s.state = 'presented')))
BEGIN SELECT RAISE(ABORT, 'invalid notification state transition'); END;
CREATE TRIGGER notification_state_no_update BEFORE UPDATE ON notification_state
BEGIN SELECT RAISE(ABORT, 'notification states are append-only'); END;
CREATE TRIGGER notification_state_no_delete BEFORE DELETE ON notification_state
BEGIN SELECT RAISE(ABORT, 'notification states are append-only'); END;
CREATE TABLE notification_delivery (
    id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES notification(id),
    adapter TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    outcome TEXT NOT NULL CHECK (outcome IN ('delivered', 'failed')),
    error_code TEXT,
    attempted_utc TEXT NOT NULL,
    UNIQUE (notification_id, adapter, attempt),
    CHECK ((outcome = 'failed') = (error_code IS NOT NULL))
);
CREATE TRIGGER notification_delivery_no_update BEFORE UPDATE ON notification_delivery
BEGIN SELECT RAISE(ABORT, 'notification deliveries are append-only'); END;
CREATE TRIGGER notification_delivery_no_delete BEFORE DELETE ON notification_delivery
BEGIN SELECT RAISE(ABORT, 'notification deliveries are append-only'); END;
"""

# P6.2b.4 (registered in P6.2b.5). Additive only (no v1 - v5 table is altered):
# - notification_claim (M-6): a delivery attempt is owned by exactly one worker before the
#   adapter is called; a delivery row needs its claim; at most one successful delivery.
# - trade_broker_link / reconciliation_discrepancy (M-9): user-only link decisions
#   (CONFIRMED / REJECTED / REVOKED, exclusive per trade and per position) and persisted
#   discrepancy findings.
# - evidence_record / evidence_link (C-12): immutable, keyed (HMAC) evidence with
#   correction links, associated with decisions and risk assessments.
# Registered in P6.2b.5 (unchanged DDL from P6.2b.4). The next backend start migrates a v5
# journal to v6 after a verified pre-migration backup; installing the tables activates nothing.
SCHEMA_V6 = """
CREATE TABLE notification_claim (
    id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES notification(id),
    adapter TEXT NOT NULL CHECK (length(adapter) BETWEEN 1 AND 60),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    worker TEXT NOT NULL CHECK (length(worker) BETWEEN 1 AND 80),
    claimed_utc TEXT NOT NULL,
    UNIQUE (notification_id, adapter, attempt)
);
CREATE INDEX notification_claim_by_notification ON notification_claim (notification_id);
CREATE TRIGGER notification_claim_no_update BEFORE UPDATE ON notification_claim
BEGIN SELECT RAISE(ABORT, 'notification claims are append-only'); END;
CREATE TRIGGER notification_claim_no_delete BEFORE DELETE ON notification_claim
BEGIN SELECT RAISE(ABORT, 'notification claims are append-only'); END;
-- A delivery outcome must belong to a claimed attempt, and a notification is delivered once.
CREATE TRIGGER notification_delivery_needs_claim BEFORE INSERT ON notification_delivery
WHEN NOT EXISTS (SELECT 1 FROM notification_claim c WHERE c.notification_id =
    NEW.notification_id AND c.adapter = NEW.adapter AND c.attempt = NEW.attempt)
BEGIN SELECT RAISE(ABORT, 'a delivery outcome needs its claimed attempt'); END;
CREATE UNIQUE INDEX notification_delivered_once ON notification_delivery (notification_id)
    WHERE outcome = 'delivered';

CREATE TABLE trade_broker_link (
    id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trade(id),
    position_ref TEXT NOT NULL CHECK (length(position_ref) = 64),
    observation_id TEXT NOT NULL REFERENCES broker_observation(id),
    decision TEXT NOT NULL CHECK (decision IN ('CONFIRMED', 'REJECTED', 'REVOKED')),
    supersedes_id TEXT UNIQUE REFERENCES trade_broker_link(id),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    actor TEXT NOT NULL CHECK (actor = 'user'),
    decided_utc TEXT NOT NULL,
    CHECK ((decision = 'REVOKED') = (supersedes_id IS NOT NULL))
);
CREATE INDEX trade_broker_link_by_trade ON trade_broker_link (trade_id);
CREATE INDEX trade_broker_link_by_position ON trade_broker_link (position_ref);
-- Exclusive: one active confirmation per position and per trade (active = not revoked).
CREATE TRIGGER trade_broker_link_exclusive BEFORE INSERT ON trade_broker_link
WHEN NEW.decision = 'CONFIRMED' AND EXISTS (SELECT 1 FROM trade_broker_link c
    WHERE c.decision = 'CONFIRMED'
      AND (c.position_ref = NEW.position_ref OR c.trade_id = NEW.trade_id)
      AND NOT EXISTS (SELECT 1 FROM trade_broker_link r WHERE r.supersedes_id = c.id))
BEGIN SELECT RAISE(ABORT, 'position or trade already has an active confirmed link'); END;
-- A revocation names the confirmation it ends (same trade and position).
CREATE TRIGGER trade_broker_link_revocation BEFORE INSERT ON trade_broker_link
WHEN NEW.decision = 'REVOKED' AND NOT EXISTS (SELECT 1 FROM trade_broker_link c
    WHERE c.id = NEW.supersedes_id AND c.decision = 'CONFIRMED'
      AND c.trade_id = NEW.trade_id AND c.position_ref = NEW.position_ref)
BEGIN SELECT RAISE(ABORT, 'a revocation must name an active confirmed link'); END;
CREATE TRIGGER trade_broker_link_no_update BEFORE UPDATE ON trade_broker_link
BEGIN SELECT RAISE(ABORT, 'trade links are append-only'); END;
CREATE TRIGGER trade_broker_link_no_delete BEFORE DELETE ON trade_broker_link
BEGIN SELECT RAISE(ABORT, 'trade links are append-only'); END;

CREATE TABLE reconciliation_discrepancy (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES broker_observation(id),
    code TEXT NOT NULL CHECK (code IN ('LOGBOOK_CLOSED_BROKER_OPEN',
        'BROKER_CLOSED_LOGBOOK_OPEN', 'IDENTIFIER_CONFLICT', 'SYMBOL_MISMATCH',
        'DIRECTION_MISMATCH', 'VOLUME_MISMATCH', 'MULTIPLE_POSSIBLE_MATCHES',
        'LINKED_POSITION_NOT_OBSERVED')),
    subject_key TEXT NOT NULL,
    trade_id TEXT REFERENCES trade(id),
    position_ref TEXT,
    detail TEXT NOT NULL CHECK (json_valid(detail) AND json_type(detail) = 'object'),
    recorded_utc TEXT NOT NULL,
    CHECK (subject_key = COALESCE(trade_id, position_ref)),
    UNIQUE (observation_id, code, subject_key)
);
CREATE INDEX reconciliation_discrepancy_by_trade ON reconciliation_discrepancy (trade_id);
CREATE TRIGGER reconciliation_discrepancy_no_update BEFORE UPDATE ON reconciliation_discrepancy
BEGIN SELECT RAISE(ABORT, 'discrepancies are append-only'); END;
CREATE TRIGGER reconciliation_discrepancy_no_delete BEFORE DELETE ON reconciliation_discrepancy
BEGIN SELECT RAISE(ABORT, 'discrepancies are append-only'); END;

CREATE TABLE evidence_record (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('market_data', 'broker_terms', 'broker_observation',
        'cost_evidence', 'reconciliation', 'readiness', 'analysis', 'risk_input', 'other')),
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 200),
    symbol TEXT CHECK (symbol IS NULL OR symbol IN ('EURUSD', 'GBPUSD', 'USDJPY')),
    topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 120),
    status TEXT NOT NULL CHECK (status IN ('VERIFIED', 'UNVERIFIED')),
    observed_utc TEXT NOT NULL,
    valid_until_utc TEXT,
    content TEXT NOT NULL CHECK (json_valid(content) AND json_type(content) = 'object'),
    content_fingerprint TEXT NOT NULL UNIQUE,
    mac TEXT NOT NULL CHECK (length(mac) = 64),
    supersedes_id TEXT UNIQUE REFERENCES evidence_record(id),
    correction_reason TEXT CHECK (correction_reason IS NULL
        OR length(correction_reason) BETWEEN 1 AND 500),
    actor TEXT NOT NULL CHECK (actor IN ('user', 'johanna')),
    recorded_utc TEXT NOT NULL,
    CHECK ((supersedes_id IS NULL) = (correction_reason IS NULL)),
    CHECK (valid_until_utc IS NULL OR valid_until_utc >= observed_utc)
);
CREATE INDEX evidence_record_by_topic ON evidence_record (kind, symbol, topic);
CREATE TRIGGER evidence_record_no_update BEFORE UPDATE ON evidence_record
BEGIN SELECT RAISE(ABORT, 'evidence records are immutable'); END;
CREATE TRIGGER evidence_record_no_delete BEFORE DELETE ON evidence_record
BEGIN SELECT RAISE(ABORT, 'evidence records are immutable'); END;
CREATE TABLE evidence_link (
    id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidence_record(id),
    subject TEXT NOT NULL CHECK (subject IN ('decision', 'risk_assessment')),
    subject_key TEXT NOT NULL,
    decision_id TEXT REFERENCES decision(id),
    risk_assessment_id TEXT REFERENCES risk_assessment(id),
    actor TEXT NOT NULL CHECK (actor IN ('user', 'johanna')),
    linked_utc TEXT NOT NULL,
    CHECK ((subject = 'decision') = (decision_id IS NOT NULL)),
    CHECK ((subject = 'risk_assessment') = (risk_assessment_id IS NOT NULL)),
    CHECK (subject_key = COALESCE(decision_id, risk_assessment_id)),
    UNIQUE (evidence_id, subject, subject_key)
);
CREATE INDEX evidence_link_by_subject ON evidence_link (subject, subject_key);
CREATE TRIGGER evidence_link_no_update BEFORE UPDATE ON evidence_link
BEGIN SELECT RAISE(ABORT, 'evidence links are append-only'); END;
CREATE TRIGGER evidence_link_no_delete BEFORE DELETE ON evidence_link
BEGIN SELECT RAISE(ABORT, 'evidence links are append-only'); END;
"""
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
    (3, SCHEMA_V3),
    (4, SCHEMA_V4),
    (5, SCHEMA_V5),  # Phase 2: registered (corrected staged v5, unchanged DDL)
    (6, SCHEMA_V6),  # P6.2b.5: registered (staged in P6.2b.4, unchanged DDL)
)


def migration_sequence_valid(migrations: Sequence[tuple[int, str]]) -> bool:
    """A usable sequence: versions exactly 1..N, each once, in order (checked before any
    backup or migration, so a duplicate or gap can never leave a partly migrated journal)."""
    return [v for v, _ in migrations] == list(range(1, len(migrations) + 1))


def tables_installed(conn: sqlite3.Connection, *names: str) -> bool:
    """True when every named table exists (staged features report ``not_installed`` else)."""
    found = {
        r[0]
        for r in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            f"({','.join('?' * len(names))})",
            names,
        )
    }
    return found == set(names)


class JournalError(RuntimeError):
    """The journal refused an operation. ``code`` is safe to show; no key material ever."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.replace("_", " "))


# --------------------------------------------------------------------------- helpers
def utc_iso(value: datetime) -> str:
    """UTC ISO-8601 with microseconds and ``Z`` (the audit timestamp representation)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def audit_row_hash(fields: Sequence[Any]) -> str:
    """SHA-256 of the canonical audit row (see module docstring)."""
    data = json.dumps(list(fields), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now: datetime) -> str:
    """26-character ULID (48-bit millisecond time + 80 random bits), stdlib only."""
    value = (int(now.timestamp() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def key_check_value(key: bytes) -> str:
    return hmac.new(key, KEY_CHECK_MESSAGE, hashlib.sha256).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- ACL (A10)
def _system32(tool: str) -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", tool)


def _run(args: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable, explicit arguments, no shell
        args,
        shell=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise JournalError("acl_failed", "user-only permissions could not be applied")
    return result.stdout


def _windows_user() -> tuple[str, str]:
    out = _run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"]).strip()
    match = re.fullmatch(r'"(?P<name>[^"]+)","(?P<sid>S-[0-9-]+)"', out)
    if not match:
        raise JournalError("acl_failed", "the current Windows user could not be determined")
    return match["name"], match["sid"]


def _icacls_entries(output: str, path: Path) -> list[str]:
    lines = output.splitlines()
    entries: list[str] = []
    for index, line in enumerate(lines):
        if not line.strip():
            break
        text = line[len(str(path)) :] if index == 0 and line.startswith(str(path)) else line
        entries.append(text.strip())
    return [entry for entry in entries if entry]


def restrict_to_current_user(path: Path, *, directory: bool) -> None:
    """Grant access to the current user only and verify by reading the ACL back (A10)."""
    if os.name == "nt":
        name, sid = _windows_user()
        rights = "(OI)(CI)(F)" if directory else "(F)"
        _run([_system32("icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:{rights}"])
        entries = _icacls_entries(_run([_system32("icacls.exe"), str(path)]), path)
        expected = f"{name.lower()}:{rights.lower()}"
        if len(entries) != 1 or entries[0].lower() != expected:
            raise JournalError("acl_failed", "user-only permissions could not be verified")
        return
    os.chmod(path, 0o700 if directory else 0o600)
    verify_user_only(path, directory=directory)


def verify_user_only(path: Path, *, directory: bool) -> None:
    """Read-only check that ``path`` is user-only (no modification)."""
    if os.name == "nt":
        name, _ = _windows_user()
        rights = "(OI)(CI)(F)" if directory else "(F)"
        entries = _icacls_entries(_run([_system32("icacls.exe"), str(path)]), path)
        if len(entries) != 1 or entries[0].lower() != f"{name.lower()}:{rights.lower()}":
            raise JournalError("acl_failed", "permissions are not user-only")
        return
    mode = path.stat().st_mode & 0o777
    if mode != (0o700 if directory else 0o600):
        raise JournalError("acl_failed", "permissions are not user-only")


AclFunction = Callable[[Path, bool], None]


def _default_acl(path: Path, directory: bool) -> None:
    restrict_to_current_user(path, directory=directory)


def _default_acl_check(path: Path, directory: bool) -> None:
    verify_user_only(path, directory=directory)


# --------------------------------------------------------------------------- store
class JournalStore:
    """Fail-closed SQLite journal with HMAC key, triggers, audit chain and backups.

    States: ``stopped``, ``ready`` (decisions usable; ``hmac_available`` says whether the
    key verified) and ``refused`` (with a reason code). ``start`` never raises, so a journal
    problem never prevents the rest of Johanna from starting.
    """

    name = "trading_journal"

    def __init__(
        self,
        data_dir: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        acl: AclFunction | None = None,
        acl_check: AclFunction | None = None,
        migrations: Sequence[tuple[int, str]] = MIGRATIONS,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._db = self.data_dir / DB_NAME
        self._key_path = self.data_dir / KEY_NAME
        self._clock = clock or (lambda: datetime.now(UTC))
        self._acl = acl or _default_acl
        self._acl_check = acl_check or _default_acl_check
        self._migrations = tuple(sorted(migrations))
        self._fault = fault or (lambda step: None)
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = "stopped"
        self._reason: str | None = None
        self._key: bytes | None = None
        self._key_status = "unknown"
        self._init_failed = False
        self._created_now = False

    @property
    def latest_version(self) -> int:
        return self._migrations[-1][0] if self._migrations else 0

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        with self._state_lock:
            if self._init_failed:
                self._refuse("initialisation_failed")
                return
            try:
                self._open()
            except JournalError as exc:
                self._refuse(exc.code)
            except Exception as exc:  # never leak details (paths, data) into logs
                LOGGER.error("trading journal refused (%s)", type(exc).__name__)
                self._refuse("unexpected_error")

    def stop(self) -> None:
        with self._state_lock:
            if self._state == "ready":
                with contextlib.suppress(sqlite3.Error):
                    with contextlib.closing(sqlite3.connect(self._db, timeout=10)) as conn:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._state = "stopped"
            self._key = None

    def _refuse(self, code: str) -> None:
        self._state, self._reason, self._key = "refused", code, None
        LOGGER.error("trading journal refused: %s", code)

    @property
    def ready(self) -> bool:
        return self._state == "ready"

    @property
    def hmac_available(self) -> bool:
        return self.ready and self._key_status == "verified" and self._key is not None

    def status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self._state,
            "reason": self._reason,
            "schema_version": self.latest_version,
            "key_status": self._key_status,
            "hmac_available": self.hmac_available,
            # The verified key is only a prerequisite; the capabilities arrive in P5.3/P5.4.
            "hmac_prerequisite": "verified" if self.hmac_available else "refused",
            # B7.2 wording: the journal stores monitoring records but does not know whether
            # monitoring is active; that state is reported by the broker monitoring service.
            "monitoring_and_reconciliation": "reported_by_monitoring_service",
            "stale_backup_temp_dirs": len(self._temp_dirs()),
        }

    # -- opening -----------------------------------------------------------------------
    def _journal_dirs(self) -> list[Path]:
        """Published or temporary backup directories: only the journal's own prefixes."""
        return sorted(self.data_dir.glob(f"{PAIR_PREFIX}*")) + self._temp_dirs()

    def _temp_dirs(self) -> list[Path]:
        return sorted(self.data_dir.glob(f"{TMP_PREFIX}*")) if self.data_dir.exists() else []

    def _sidecars(self) -> list[Path]:
        return [Path(f"{self._db}{suffix}") for suffix in SIDECARS]

    def _open(self) -> None:
        if not migration_sequence_valid(self._migrations):
            raise JournalError("migration_order")
        if not self._db.exists():
            if self._key_path.exists():
                raise JournalError("orphaned_key")
            if self.data_dir.exists() and self._journal_dirs():
                raise JournalError("restore_required")
            if any(p.exists() for p in self._sidecars()):
                raise JournalError("stale_sidecars")
            self._first_creation()
        else:
            self._open_existing()
        self._state, self._reason = "ready", None

    def _first_creation(self) -> None:
        """Genuine first creation (§5.3). On failure remove only what this attempt created."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        conn: sqlite3.Connection | None = None
        try:
            key = secrets.token_bytes(KEY_BYTES)
            _write_exclusive(self._key_path, key)
            created.append(self._key_path)
            self._fault("after_key")
            self._acl(self._key_path, False)
            self._fault("after_acl")
            _write_exclusive(self._db, b"")  # proves this attempt created the database file
            created.append(self._db)
            created.extend(self._sidecars())  # absent before (checked); any now are ours
            conn = self._connect()
            conn.execute("PRAGMA journal_mode = WAL")
            self._fault("after_db_file")
            for version, sql in self._migrations:
                self._apply_migration(conn, version, sql)
            now = self._clock()
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("key_check", key_check_value(key)),
                    ("created_utc", utc_iso(now)),
                    ("journal_format", "1"),
                ],
            )
            self.append_audit(
                conn,
                action="journal.created",
                table="meta",
                record_id=None,
                new={"schema_version": self.latest_version},
                at=now,
            )
            conn.execute("COMMIT")
            self._fault("after_meta")
            self._check_integrity(conn)
        except BaseException as exc:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
                conn = None
            self._init_failed = True
            self._cleanup(created)
            if isinstance(exc, JournalError) and exc.code == "acl_failed":
                raise JournalError("initialisation_failed", "key permissions failed") from None
            raise JournalError("initialisation_failed") from None
        finally:
            if conn is not None:
                conn.close()
        self._key, self._key_status, self._created_now = key, "verified", True

    @staticmethod
    def _cleanup(created: list[Path]) -> None:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A leftover key makes every later start refuse ("orphaned_key"): fail closed.
                LOGGER.error("trading journal: cleanup of a failed initialisation incomplete")

    def _open_existing(self) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                self._check_integrity(conn)
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version == 0 or version > self.latest_version:
                    raise JournalError("schema_unsupported")
                row = conn.execute("SELECT value FROM meta WHERE key = 'key_check'").fetchone()
        except sqlite3.DatabaseError:
            raise JournalError("corrupt_database") from None
        if row is None:
            raise JournalError("corrupt_database")
        self._verify_key(row[0])
        pending = [(v, sql) for v, sql in self._migrations if v > version]
        if pending:
            if self._key_status != "verified":
                raise JournalError("migration_requires_verified_key")
            pair = self.backup(f"premigration-v{version}", _allow_before_ready=True)
            if not validate_pair(pair):  # manifest, hashes, key pairing, integrity, audit chain
                raise JournalError("backup_unverified")
            with contextlib.closing(self._connect()) as conn:
                for target, sql in pending:
                    self._apply_migration(conn, target, sql)
                    self._fault(f"after_migration_{target}")

    def _verify_key(self, stored_check: str) -> None:
        self._key = None
        if not self._key_path.exists():
            self._key_status = "missing"
            return
        try:
            key = self._key_path.read_bytes()
        except OSError:
            self._key_status = "unreadable"
            return
        if len(key) != KEY_BYTES or not hmac.compare_digest(key_check_value(key), stored_check):
            self._key_status = "mismatch"
            return
        try:
            self._acl_check(self._key_path, False)
        except JournalError:
            self._key_status = "permissions_not_user_only"
            return
        self._key, self._key_status = key, "verified"

    def _apply_migration(self, conn: sqlite3.Connection, version: int, sql: str) -> None:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != current + 1:
            raise JournalError("migration_order")
        # SQLite's documented procedure for schema changes (including table rebuilds): foreign
        # key enforcement is switched off outside the transaction, and ``foreign_key_check``
        # must report no violation before COMMIT. Any error or violation rolls back everything.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            statements = migration_statements(sql)  # refuses transaction control up front
            conn.execute("BEGIN IMMEDIATE")
            for statement in statements:
                conn.execute(statement)
                if not conn.in_transaction:  # a migration may never commit on its own
                    raise sqlite3.OperationalError("migration ended its own transaction")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("foreign key violation after migration")
            conn.execute(f"PRAGMA user_version = {int(version)}")
            self._fault(f"before_commit_{version}")  # test hook: last point before COMMIT
            conn.execute("COMMIT")
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise JournalError("migration_failed") from None
        except BaseException:
            # Not only SQLite errors: anything between BEGIN and COMMIT is rolled back
            # explicitly, never left to the connection being closed.
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _check_integrity(conn: sqlite3.Connection) -> None:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError:
            raise JournalError("corrupt_database") from None
        if result is None or result[0] != "ok":
            raise JournalError("corrupt_database")

    # -- connections -------------------------------------------------------------------
    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(path or self._db, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _require_ready(self) -> None:
        if not self.ready:
            raise JournalError("journal_unavailable")

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One short write transaction under the process-wide write lock."""
        self._require_ready()
        with self._write_lock, contextlib.closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        self._require_ready()
        with contextlib.closing(self._connect()) as conn:
            yield conn

    def now(self) -> datetime:
        return self._clock()

    # -- audit -------------------------------------------------------------------------
    def append_audit(
        self,
        conn: sqlite3.Connection,
        *,
        action: str,
        table: str,
        record_id: str | None,
        old: Any = None,
        new: Any = None,
        at: datetime,
        actor: str = "johanna",
    ) -> int:
        last = conn.execute(
            "SELECT seq, row_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev = last["row_hash"] if last else GENESIS_HASH
        fields = (
            seq,
            utc_iso(at),
            actor,
            action,
            table,
            record_id,
            canonical_json(old),
            canonical_json(new),
            prev,
        )
        conn.execute(
            "INSERT INTO audit_log(seq, recorded_utc, actor, action, table_name, record_id, "
            "old_values, new_values, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*fields, audit_row_hash(fields)),
        )
        return seq

    def verify_audit_chain(self) -> dict[str, object]:
        with self.read() as conn:
            return verify_audit_rows(
                conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
            )

    # -- HMAC references (A4; used by later milestones) --------------------------------
    def broker_ref(self, kind: str, raw_id: object) -> str:
        if not self.hmac_available or self._key is None:
            raise JournalError("hmac_unavailable")
        if kind not in ("position", "deal", "order"):
            raise ValueError("unknown reference kind")
        # M-2: a missing identifier must never become a shared synthetic reference.
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            raise ValueError("broker identifier missing or invalid")
        return hmac.new(self._key, f"{kind}:{raw_id}".encode(), hashlib.sha256).hexdigest()

    def keyed_digest(self, purpose: str, data: bytes) -> str:
        """HMAC-SHA-256 with the verified journal key, domain-separated by ``purpose`` (C-12
        evidence MACs). Refuses without a verified key; the key itself never leaves."""
        if not self.hmac_available or self._key is None:
            raise JournalError("hmac_unavailable")
        if not re.fullmatch(r"[a-z][a-z0-9_.]{0,40}", purpose):
            raise ValueError("invalid digest purpose")
        message = purpose.encode() + b"\x00" + data
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    # -- backups (§5.4) ----------------------------------------------------------------
    def backup(self, kind: str = "daily", *, _allow_before_ready: bool = False) -> Path:
        """WAL-safe backup pair built privately and published by one directory rename."""
        if not _allow_before_ready:
            self._require_ready()
        if kind != "daily" and not re.fullmatch(r"premigration-v\d+", kind):
            raise ValueError("unknown backup kind")
        if self._key is None:
            raise JournalError("backup_requires_verified_key")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        final = self.data_dir / f"{PAIR_PREFIX}{kind}-{stamp}"
        if final.exists():
            raise JournalError("backup_failed")
        temp = self.data_dir / f"{TMP_PREFIX}{stamp}-{secrets.token_hex(4)}"
        temp.mkdir()  # exclusive: fails if it exists
        try:
            snapshot = temp / DB_NAME
            with contextlib.closing(sqlite3.connect(self._db, timeout=10)) as source:
                with contextlib.closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)  # online backup API: consistent, WAL-safe snapshot
            self._fault("backup_after_snapshot")
            with contextlib.closing(sqlite3.connect(snapshot, isolation_level=None)) as conn:
                conn.execute("PRAGMA journal_mode = DELETE")  # self-contained single file
                self._check_integrity(conn)
                conn.row_factory = sqlite3.Row
                stored = conn.execute("SELECT value FROM meta WHERE key='key_check'").fetchone()
                snapshot_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                audit = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
            if not verify_audit_rows(audit)["valid"]:
                raise JournalError("backup_audit_invalid")
            key_bytes = self._key_path.read_bytes()
            _write_exclusive(temp / KEY_NAME, key_bytes)
            self._fault("backup_after_key")
            if stored is None or not hmac.compare_digest(key_check_value(key_bytes), stored[0]):
                raise JournalError("backup_key_mismatch")
            self._acl(temp / KEY_NAME, False)
            self._acl(temp, True)
            self._fault("backup_after_acl")
            manifest = {
                "format": 1,
                "kind": kind,
                "created_utc": stamp,
                "schema_version": snapshot_version,
                "key_check": stored[0],
                "files": {DB_NAME: _sha256_file(snapshot), KEY_NAME: _sha256_file(temp / KEY_NAME)},
                "verified": True,
            }
            _write_exclusive(temp / PAIR_MANIFEST, json.dumps(manifest, sort_keys=True).encode())
            self._fault("backup_before_publish")
            if final.exists():
                raise JournalError("backup_failed")
            os.rename(temp, final)  # one same-filesystem rename publishes the complete pair
        except BaseException as exc:
            shutil.rmtree(temp, ignore_errors=True)  # only this attempt's temporary directory
            code = exc.code if isinstance(exc, JournalError) else "backup_failed"
            raise JournalError(code) from None
        return final

    def valid_pairs(self) -> list[Path]:
        return [
            path for path in sorted(self.data_dir.glob(f"{PAIR_PREFIX}*")) if validate_pair(path)
        ]

    def prune_daily(self, keep: int = DAILY_RETENTION) -> list[Path]:
        """Delete the oldest *valid* daily pairs beyond ``keep``; never pre-migration pairs,
        never invalid or incomplete directories."""
        daily = [p for p in self.valid_pairs() if _PAIR_NAME.fullmatch(p.name)[1] == "daily"]
        daily.sort(key=lambda p: _PAIR_NAME.fullmatch(p.name)[2])
        removed = daily[:-keep] if keep > 0 and len(daily) > keep else []
        for path in removed:
            shutil.rmtree(path)
        return removed


_TRANSACTION_CONTROL = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "VACUUM", "ATTACH", "DETACH"}
)


def migration_statements(sql: str) -> list[str]:
    """Split a migration into complete SQL statements (``sqlite3.complete_statement`` decides,
    so semicolons inside trigger bodies and string literals stay together). A migration may
    not control its own transaction, and an incomplete trailing statement is an error."""
    code_only = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ).rstrip()
    if code_only and not code_only.endswith(";"):
        raise sqlite3.OperationalError("incomplete statement at the end of the migration")
    statements: list[str] = []
    buffer = ""
    for piece in sql.split(";"):
        buffer += piece + ";"
        if not sqlite3.complete_statement(buffer):
            continue
        code = "\n".join(
            line for line in buffer.splitlines() if not line.strip().startswith("--")
        ).strip(" \t\r\n;")
        buffer = ""
        if not code:
            continue
        if code.split(None, 1)[0].upper() in _TRANSACTION_CONTROL:
            raise sqlite3.OperationalError("a migration may not control its own transaction")
        statements.append(code + ";")
    if buffer.strip(" \t\r\n;"):
        raise sqlite3.OperationalError("incomplete statement at the end of the migration")
    return statements


def validate_pair(path: Path) -> bool:
    """A valid pair: published name, ``pair.json`` verified, hashes and key check matching,
    and an intact database. Anything else is never a backup."""
    try:
        if not _PAIR_NAME.fullmatch(path.name) or not path.is_dir():
            return False
        manifest = json.loads((path / PAIR_MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("verified") is not True:
            return False
        files = manifest.get("files") or {}
        db, key = path / DB_NAME, path / KEY_NAME
        if files.get(DB_NAME) != _sha256_file(db) or files.get(KEY_NAME) != _sha256_file(key):
            return False
        key_bytes = key.read_bytes()
        if len(key_bytes) != KEY_BYTES or key_check_value(key_bytes) != manifest.get("key_check"):
            return False
        uri = f"file:{db.as_posix()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
            stored = conn.execute("SELECT value FROM meta WHERE key='key_check'").fetchone()
            conn.row_factory = sqlite3.Row
            audit = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        if not verify_audit_rows(audit)["valid"]:
            return False
        return stored is not None and stored[0] == manifest["key_check"]
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def verify_audit_rows(
    rows: Sequence[sqlite3.Row | dict], *, start_seq: int = 1, previous: str = GENESIS_HASH
) -> dict[str, object]:
    """Verify a full audit chain (tamper-evident only; see module docstring). ``start_seq`` /
    ``previous`` let a caller verify only the rows after an already verified anchor (M-8);
    the result then covers those rows only."""
    for expected_seq, row in enumerate(rows, start=start_seq):
        seq = row["seq"]
        if seq != expected_seq:
            return {
                "valid": False,
                "rows": len(rows),
                "first_bad_seq": seq,
                "problem": "sequence gap or reordering",
            }
        if row["prev_hash"] != previous:
            return {
                "valid": False,
                "rows": len(rows),
                "first_bad_seq": seq,
                "problem": "previous hash mismatch",
            }
        fields = (
            seq,
            row["recorded_utc"],
            row["actor"],
            row["action"],
            row["table_name"],
            row["record_id"],
            row["old_values"],
            row["new_values"],
            row["prev_hash"],
        )
        if audit_row_hash(fields) != row["row_hash"]:
            return {
                "valid": False,
                "rows": len(rows),
                "first_bad_seq": seq,
                "problem": "row hash mismatch",
            }
        previous = row["row_hash"]
    return {
        "valid": True,
        "rows": len(rows),
        "first_bad_seq": None,
        "problem": None,
        "guarantee": "tamper-evident only; no external anchor",
    }
