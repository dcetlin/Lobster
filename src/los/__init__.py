"""
LOS — Life Operating System

Cycle 1: personal action item extraction and surfacing.

Components:
    db         — schema, CRUD, dedup for self_action_items.db
    extractor  — LLM-powered extraction from text (telegram, voice notes)
    callbacks  — Telegram inline button callback routing (done/snooze/dismiss)
    digest     — morning digest section builder
"""
