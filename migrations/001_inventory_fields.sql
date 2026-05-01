-- Migration 001: Add inventory fields to miners table
-- Generated: 2026-04-18
-- Safe to run multiple times (uses IF NOT EXISTS / DO NOTHING pattern via ALTER COLUMN)

ALTER TABLE miners
    ADD COLUMN IF NOT EXISTS client_name          VARCHAR(128),
    ADD COLUMN IF NOT EXISTS box_serial           VARCHAR(64),
    ADD COLUMN IF NOT EXISTS psu_serial           VARCHAR(64),
    ADD COLUMN IF NOT EXISTS nameplate_hashrate   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS inventory_location   VARCHAR(256),
    ADD COLUMN IF NOT EXISTS inventory_subunit    VARCHAR(64),
    ADD COLUMN IF NOT EXISTS inventory_source     VARCHAR(64),
    ADD COLUMN IF NOT EXISTS inventory_notes      TEXT,
    ADD COLUMN IF NOT EXISTS inventory_only       BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS historical_names     JSON,
    ADD COLUMN IF NOT EXISTS inventory_imported_at TIMESTAMP;

-- Shelf scan tracking columns on racks
ALTER TABLE racks
  ADD COLUMN IF NOT EXISTS expected_count INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_scan_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS last_scan_found INTEGER,
  ADD COLUMN IF NOT EXISTS scan_in_progress BOOLEAN DEFAULT FALSE;

-- Columns referenced by miner_model but missing from original schema
ALTER TABLE miners
  ADD COLUMN IF NOT EXISTS ip_history JSON,
  ADD COLUMN IF NOT EXISTS location VARCHAR(64),
  ADD COLUMN IF NOT EXISTS smart_test_completed BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS test_count INTEGER DEFAULT 0;
