---
name: database
description: Relational databases in practice - schema design, indexes and query plans, migrations, transactions and locking, backups/restores, and CLI usage for SQLite, PostgreSQL and MySQL/MariaDB, plus Redis basics. Use when designing tables, writing or optimizing SQL, migrating data, or diagnosing a slow or broken database.
keywords: sql, postgres, postgresql, mysql, mariadb, sqlite, データベース, スキーマ, マイグレーション, クエリ, インデックス設計
---

# Databases

## Schema design
- Every table: a primary key (`bigint` identity or UUIDv7), `created_at`/`updated_at` timestamps in UTC (`timestamptz` in Postgres).
- Normalize first (no repeated groups, no comma-separated lists in a column); denormalize only with a measured reason.
- Foreign keys with explicit `ON DELETE` behavior; `NOT NULL` by default; CHECK constraints for enums/ranges; unique constraints for natural keys (email lowercased).
- Money as integer minor units or `numeric(12,2)`, never float. Text: `text`/`varchar` with sensible limits; store UTF-8 (`utf8mb4` in MySQL).

## Indexes and performance
- Index columns used in WHERE/JOIN/ORDER BY of frequent queries; composite index column order = equality columns first, then range/sort.
- Read the plan before and after: Postgres `EXPLAIN (ANALYZE, BUFFERS) <query>`, MySQL `EXPLAIN ANALYZE`, SQLite `EXPLAIN QUERY PLAN`. Look for sequential scans on big tables and row estimate mismatches.
- N+1 queries from ORMs: fetch related rows in one query (JOIN, `selectinload`, `include`).
- Pagination: keyset (`WHERE id > :last ORDER BY id LIMIT 50`) beats OFFSET on large tables.

## Migrations
- One migration tool per project (Alembic, Prisma, Django, Flyway, Knex/Drizzle). Never edit an applied migration; add a new one.
- Zero-downtime pattern: add nullable column -> backfill in batches -> add constraint/NOT NULL -> switch code -> drop old column later.
- Postgres: create indexes `CONCURRENTLY` on live tables; adding a column with a constant default is cheap since PG 11.

## Transactions and safety
- Wrap multi-statement changes in a transaction; keep transactions short.
- Before UPDATE/DELETE without obvious bounds: run the same WHERE as a SELECT COUNT first, then execute inside a transaction and check the affected row count before COMMIT.
- Backups before risky changes: `pg_dump -Fc -f db.dump dbname` (restore `pg_restore -d dbname db.dump`), `mysqldump --single-transaction --routines db > db.sql`, SQLite `sqlite3 app.db ".backup app.bak.db"` (safe while in use).

## CLI quick reference
- SQLite: `sqlite3 app.db ".tables" ".schema t" "SELECT ...;"`; enable WAL for concurrent readers: `PRAGMA journal_mode=WAL;`; foreign keys are off unless `PRAGMA foreign_keys=ON;` per connection.
- PostgreSQL: `psql "postgresql://user@host/db" -c "\dt"`, `\d table`, `\x` expanded output; non-interactive: `psql -X -A -t -c "..."`; password via `PGPASSWORD` env or `~/.pgpass`, not arguments. Activity: `SELECT pid, state, wait_event, query FROM pg_stat_activity;` locks: `pg_locks`.
- MySQL/MariaDB: `mysql -h host -u user -p db -e "SHOW TABLES;"`; credentials in `~/.my.cnf`; `SHOW PROCESSLIST;`, `SHOW ENGINE INNODB STATUS\G`.
- Redis: `redis-cli PING`, `INFO memory`, use `SCAN` not `KEYS *` in production; set TTLs on cache keys.

## Diagnosing
- Slow: find the query (pg_stat_statements, slow query log), EXPLAIN it, check indexes and row counts, check locks/blocking.
- Connection errors: is the server listening (`ss -ltnp`), firewall, `pg_hba.conf`/bind-address, max_connections, credentials.
- Disk full: check data dir, WAL/binlog growth, old backups; never delete WAL files by hand.
