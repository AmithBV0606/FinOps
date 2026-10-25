Tables are created with `Base.metadata.create_all` on first use. When the schema needs to
evolve after real data exists (e.g. moving to Postgres), add Alembic here:
`alembic init finops/db/migrations` and point `env.py` at `finops.db.models.Base.metadata`.
