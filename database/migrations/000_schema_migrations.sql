-- 迁移登记表。本文件是 runner 的引导文件（不通过 runner 应用，而是先执行它以建表）。
--
-- 与早期版本的差别：checksum 存**换行归一化后**的 sha256，消除 CRLF/LF 跨平台差异。
-- 早期版本 7/35 个迁移是 CRLF，4 份 runner 用 3 种算法，互相判漂移。

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    VARCHAR(64) NOT NULL,
  checksum   CHAR(64)    NOT NULL,
  applied_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
