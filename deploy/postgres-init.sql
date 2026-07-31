-- NiceKnowledge 开发库角色初始化
-- 扩展由 00-extensions.sql 以 postgres 预装；migrator 保持非 superuser。
-- 双账号:migrator(owner,跑 Alembic,可绕过 RLS 之外还需 FORCE 约束)/ app(应用连接,受 RLS 约束)

CREATE ROLE niceknowledge_migrator WITH LOGIN PASSWORD 'migrator-dev-secret';
CREATE ROLE niceknowledge_app WITH LOGIN PASSWORD 'app-dev-secret';

ALTER DATABASE niceknowledge OWNER TO niceknowledge_migrator;
GRANT ALL ON SCHEMA public TO niceknowledge_migrator;
GRANT USAGE ON SCHEMA public TO niceknowledge_app;

-- 应用账号默认可读写 migrator 建的表(RLS 再做行级过滤)
ALTER DEFAULT PRIVILEGES FOR ROLE niceknowledge_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO niceknowledge_app;
ALTER DEFAULT PRIVILEGES FOR ROLE niceknowledge_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO niceknowledge_app;
