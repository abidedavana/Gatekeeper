# Changelog

All notable changes to Gatekeeper are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
Semantic Versioning.

## [1.0.0] - 2026-07-05

### Added

- `check`, `audit`, `cache status` and `cache clear` CLI commands (click).
- Typosquat detection against a bundled top-150 PyPI / top-150 npm snapshot:
  Levenshtein distance ≤ 2 plus confusable-character folds (rn↔m, 0↔o, l↔1).
- Registry heuristics scored from real API fields: first-release age,
  release count, and best-effort GitHub repo-owner account age.
- Auditable 0–100 risk scoring with fixed per-signal points and inclusive
  bands (LOW 0–39, MEDIUM 40–59, HIGH 60–79, CRITICAL 80–100).
- Cleaned `manifest.safe` output; only unambiguous typos (not found on the
  registry, close to a popular name) are rewritten.
- SQLite metadata cache: WAL mode, busy retry with backoff, 24 h TTL,
  top-50 pinned entries, LRU eviction in a single-writer transaction.
- Fire-and-forget cache prefetch from a static co-occurrence map.
- `--json` output with a stable, versioned schema for CI parsing.
- Dockerfile, GitHub Actions CI (ruff + pytest on Python 3.10–3.12),
  overview slide deck and terminal screenshots from real runs.

### Fixed

- Test collection under the bare `pytest` entrypoint (`tests/` is now a
  regular package).

[1.0.0]: https://github.com/abidedavana/Gatekeeper/releases/tag/v1.0.0
