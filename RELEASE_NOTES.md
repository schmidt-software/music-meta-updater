v0.2.0 - 2026-07-25
-------------------

- Add UPDATER_WORKERS support and docs: multiple updater threads may run light
  pre-processing concurrently while beets subprocesses remain serialized to
  protect the beets SQLite DB. Default remains 1 to preserve previous safe behavior.
- Add .env.example and docker-compose.yml examples for UPDATER_WORKERS.


Unreleased changes:

- (none)
