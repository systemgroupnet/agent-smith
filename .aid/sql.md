# House rules: SQL and migrations (collector/)

- The collector's schema is owned by Alembic (`collector/migrations/versions/`),
  PostgreSQL only — no SQLite fallback.
- Migrations are written to be re-runnable: they check that a table or column
  exists before creating or altering it, because a migration can run against a
  database an older version created. A missing guard is a defect.
- A new column is nullable with no backfill unless the migration says why; NULL
  means "written before this migration". That is intended.
- Timestamps are stored as TEXT (ISO 8601) and cast with `::timestamptz` in
  queries. Not a defect.
- Queries are SQLAlchemy Core in Python. There are no database views on purpose:
  the SQL ships and versions with the code that reads it.
