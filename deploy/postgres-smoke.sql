\set ON_ERROR_STOP on

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
  ) THEN
    RAISE EXCEPTION 'extension preflight must run as a PostgreSQL superuser';
  END IF;
END
$$;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS zhparser;
SELECT format(
  'ALTER DATABASE %I SET zhparser.multi_short = true',
  current_database()
) \gexec
SET zhparser.multi_short = true;

DROP TEXT SEARCH CONFIGURATION IF EXISTS public.zhparser_smoke;
CREATE TEXT SEARCH CONFIGURATION public.zhparser_smoke (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION public.zhparser_smoke
  ADD MAPPING FOR n,v,a,i,e,l WITH simple;

DO $$
BEGIN
  IF current_setting('server_version_num')::integer / 10000 <> 17 THEN
    RAISE EXCEPTION 'expected PostgreSQL 17, got %', current_setting('server_version');
  END IF;
  IF to_tsvector('public.zhparser_smoke'::regconfig, '清迈亲子酒店') = ''::tsvector THEN
    RAISE EXCEPTION 'zhparser produced an empty tsvector';
  END IF;
  IF NOT (
    to_tsvector('public.zhparser_smoke'::regconfig, '清迈亲子酒店推荐')
    @@ websearch_to_tsquery('public.zhparser_smoke'::regconfig, '清迈 酒店')
  ) THEN
    RAISE EXCEPTION 'zhparser short-compound recall smoke test failed';
  END IF;
  IF similarity('Bangkok hotel', 'Bangkok hotels') <= 0 THEN
    RAISE EXCEPTION 'pg_trgm similarity smoke test failed';
  END IF;
  IF ('[1,2,3]'::vector <-> '[1,2,4]'::vector) <= 0 THEN
    RAISE EXCEPTION 'pgvector distance smoke test failed';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'niceknowledge_migrator' AND rolsuper
  ) THEN
    RAISE EXCEPTION 'niceknowledge_migrator must not be a superuser';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_extension AS extension
    JOIN pg_roles AS owner ON owner.oid = extension.extowner
    WHERE extension.extname IN ('vector', 'pg_trgm', 'zhparser')
      AND owner.rolname <> 'postgres'
  ) THEN
    RAISE EXCEPTION 'database extensions must remain owned by postgres';
  END IF;
END
$$;

SELECT current_setting('server_version') AS postgres_version;
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('vector', 'pg_trgm', 'zhparser')
ORDER BY extname;
SELECT to_tsvector('public.zhparser_smoke'::regconfig, '清迈亲子酒店推荐') AS zhparser_sample;

DROP TEXT SEARCH CONFIGURATION public.zhparser_smoke;
