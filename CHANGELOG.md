# Changelog

## 2026-06-09

- Stopped runaway scans from indexing Flutter/Dart cache trees and made storage-budget overruns end a crawl instead of marking large batches as pending/error records.
- Added in-memory structured logging with `/api/logs` and `/api/logs/clear`, plus a React Logs drawer with polling, copy, clear, and level coloring.
- Improved indexing/search performance with cached storage-budget scans, batch SQLite chunk staging, and bulk semantic chunk lookup.
- Hardened runtime behavior with Ollama retry/backoff and timeout configuration, serialized crawler state, safer directory traversal, auto-scan exception logging, migration race tolerance, and startup warnings for missing vector IDs.
