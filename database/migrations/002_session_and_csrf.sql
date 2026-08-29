-- ============================================================================
-- 002 会话表补齐：CSRF 原值 + 最后活跃时间
--
-- 为什么 CSRF 令牌存**原值**而不是哈希：
-- 双提交（double-submit）模式要求服务端能把它与"请求头里的值"和"Cookie 里的值"
-- 三方比对。存哈希就只能做单向校验，拿不回原值，也就无法比对 Cookie——
-- 而"以 Cookie 为基准"恰是这个模式能挡住跨站请求的原因。
--
-- 哈希方案我会更满意（库泄露也拿不到令牌），但它的可行前提是前端能读到一个
-- **独立于服务端存储**的副本，而那正是 Cookie。所以：
--     * 会话凭据（fpa_session）→ HttpOnly，服务端只存 sha256，不可还原
--     * CSRF 令牌（fpa_csrf）  → 非 HttpOnly，服务端存原值，用于比对
-- 两者安全等级不同，处理方式不同，这是刻意的。
--
-- 迁移必须**可重入**：MySQL 的 DDL 不回滚，失败后要能直接重跑。
-- 用 information_schema 判断列是否已存在，避免 "Duplicate column name"。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 用一个存储过程把"加列"写成幂等操作。
-- MySQL 没有 ADD COLUMN IF NOT EXISTS（MariaDB 有），所以只能用动态 SQL。
-- 建完立即删除，不留残留对象。
-- ---------------------------------------------------------------------------

DROP PROCEDURE IF EXISTS fpa_add_column_if_missing;

DELIMITER $$
CREATE PROCEDURE fpa_add_column_if_missing(
  IN p_table VARCHAR(64),
  IN p_column VARCHAR(64),
  IN p_definition VARCHAR(255)
)
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = p_table
      AND COLUMN_NAME = p_column
  ) THEN
    SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `', p_column, '` ', p_definition);
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END IF;
END$$
DELIMITER ;

CALL fpa_add_column_if_missing('sessions', 'csrf_token', "VARCHAR(128) NOT NULL DEFAULT '' AFTER csrf_hash");
CALL fpa_add_column_if_missing('users', 'failed_login_count', 'INT UNSIGNED NOT NULL DEFAULT 0');
CALL fpa_add_column_if_missing('users', 'locked_until', 'DATETIME NULL');
CALL fpa_add_column_if_missing('users', 'last_login_at', 'DATETIME NULL');

DROP PROCEDURE IF EXISTS fpa_add_column_if_missing;

-- ---------------------------------------------------------------------------
-- 用户的默认数据范围（个人范围）：任何新用户至少能看自己创建的数据。
--
-- 为什么给默认值：不给的话每个新建账号都要管理员手工配范围，忘了配就是
-- DATA_SCOPE_UNRESOLVED——用户能登录却什么都看不了。给一个最小可用默认值，
-- 让"忘配"的后果是"只能看自己的"，而不是"完全不能用"。
-- ---------------------------------------------------------------------------

INSERT INTO data_scopes (code, name, scope_type, status)
VALUES ('personal-default', '仅本人数据（默认）', 'personal', 'active')
ON DUPLICATE KEY UPDATE name = VALUES(name);
