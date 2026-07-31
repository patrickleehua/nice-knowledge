\set ON_ERROR_STOP on

-- docker-entrypoint-initdb.d runs this as POSTGRES_USER (postgres). zhparser is
-- untrusted and must be installed before ownership is handed to the migrator.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS zhparser;

-- zhparser marks segmentation GUCs superuser-only. Persist short compounds at
-- bootstrap so later non-superuser migration/application sessions compute the
-- same searchable vectors (for example, 酒店推荐 also emits 酒店).
SELECT format(
  'ALTER DATABASE %I SET zhparser.multi_short = true',
  current_database()
) \gexec
