# Conventions

## Import Path Conventions

Always use `from src.utils.X import Y` (not `from utils.X import Y`) for cross-package imports in any file that may be executed from the repo root (`~/lobster`). Files in `src/mcp/`, `src/bot/`, `src/delivery/`, and `src/channels/` are invoked from the repo root by MCP server entrypoints, cron, or systemd services — bare `from utils.X` imports work in tests (where `conftest.py` inserts `src/` onto sys.path) but fail at runtime.

Ref: PR #1173, oracle verdict `oracle/verdicts/archive/pr-1173.md`
