-- ============================================================================
-- 001 身份 / 组织 / RBAC / DataScope / 治理 / 审计
--
-- 设计原则（对照早期版本的教训）：
--   1. data_scopes 必须有分租键列。早期版本缺 farm_id，导致 farm 型范围
--      静默退化成 "1=0"（用户看到 0 行却不报错）——这是本项目要根除的头号缺陷。
--   2. 审计表 append-only，用触发器在数据库层强制（继承早期版本已验证的做法）。
--   3. 幂等键表**不存响应快照**。早期版本 response_json 可能 200 KB，且三次独立
--      事务会在崩溃后留下永久 processing 记录。新系统与业务写入同事务，
--      只存 resource_id，回放时从业务表读。
--   4. 所有可编辑表带 row_version（乐观锁）。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 组织与基地（DataScope 的 farm / area 两级分租键来源）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS organizations (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code          VARCHAR(64)     NOT NULL,
  name          VARCHAR(120)    NOT NULL,
  status        ENUM('active','disabled') NOT NULL DEFAULT 'active',
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_organizations_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS farms (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(120)    NOT NULL,
  status          ENUM('active','disabled','archived') NOT NULL DEFAULT 'active',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_farms_organization_code (organization_id, code),
  KEY idx_farms_organization (organization_id),
  CONSTRAINT fk_farms_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 用户 / 角色 / 权限
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username       VARCHAR(64)     NOT NULL,
  display_name   VARCHAR(120)    NOT NULL,
  password_hash  VARCHAR(255)    NOT NULL,
  status         ENUM('pending','active','disabled','retired') NOT NULL DEFAULT 'pending',
  must_change_password TINYINT(1) NOT NULL DEFAULT 0,
  row_version    INT UNSIGNED    NOT NULL DEFAULT 1,
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_username (username),
  KEY idx_users_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS roles (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code        VARCHAR(64)     NOT NULL,
  name        VARCHAR(120)    NOT NULL,
  description VARCHAR(255)    NOT NULL DEFAULT '',
  status      ENUM('active','disabled') NOT NULL DEFAULT 'active',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_roles_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS permissions (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code        VARCHAR(96)     NOT NULL,
  name        VARCHAR(120)    NOT NULL,
  domain      VARCHAR(64)     NOT NULL,
  description VARCHAR(255)    NOT NULL DEFAULT '',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_permissions_code (code),
  KEY idx_permissions_domain (domain)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS role_permissions (
  role_id       BIGINT UNSIGNED NOT NULL,
  permission_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (role_id, permission_id),
  CONSTRAINT fk_role_permissions_role FOREIGN KEY (role_id)
    REFERENCES roles(id) ON UPDATE RESTRICT ON DELETE CASCADE,
  CONSTRAINT fk_role_permissions_permission FOREIGN KEY (permission_id)
    REFERENCES permissions(id) ON UPDATE RESTRICT ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_roles (
  user_id BIGINT UNSIGNED NOT NULL,
  role_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (user_id, role_id),
  CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE CASCADE,
  CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id)
    REFERENCES roles(id) ON UPDATE RESTRICT ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- DataScope
--
-- 关键设计：四种范围类型各有**自己的分租键列**，且全部可空 —— 但应用层在解析时
-- 强制"scope_type 对应的那一列必须非空"，否则抛 DATA_SCOPE_UNRESOLVED。
-- 早期版本只有 area_id，farm 型范围无从表达，于是静默变成空集。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS data_scopes (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(120)    NOT NULL,
  scope_type      ENUM('farm','area','pond','personal') NOT NULL,
  organization_id BIGINT UNSIGNED NULL,
  farm_id         BIGINT UNSIGNED NULL,
  area_id         BIGINT UNSIGNED NULL,
  pond_id         BIGINT UNSIGNED NULL,
  status          ENUM('active','disabled') NOT NULL DEFAULT 'active',
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_data_scopes_code (code),
  KEY idx_data_scopes_type (scope_type),
  -- 约束：每种范围类型必须有对应的分租键，personal 例外（它按 created_by 判定）
  CONSTRAINT chk_data_scopes_binding CHECK (
    (scope_type = 'farm'     AND farm_id IS NOT NULL) OR
    (scope_type = 'area'     AND area_id IS NOT NULL) OR
    (scope_type = 'pond'     AND pond_id IS NOT NULL) OR
    (scope_type = 'personal')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_data_scopes (
  user_id  BIGINT UNSIGNED NOT NULL,
  scope_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (user_id, scope_id),
  CONSTRAINT fk_user_data_scopes_user FOREIGN KEY (user_id)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE CASCADE,
  CONSTRAINT fk_user_data_scopes_scope FOREIGN KEY (scope_id)
    REFERENCES data_scopes(id) ON UPDATE RESTRICT ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 会话
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sessions (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id       BIGINT UNSIGNED NOT NULL,
  token_hash    CHAR(64)        NOT NULL,          -- sha256(不透明令牌)，原始令牌不落库
  csrf_hash     CHAR(64)        NOT NULL,
  user_agent    VARCHAR(255)    NOT NULL DEFAULT '',
  ip_address    VARCHAR(45)     NOT NULL DEFAULT '',
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at    DATETIME        NOT NULL,
  revoked_at    DATETIME        NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_sessions_token_hash (token_hash),
  KEY idx_sessions_user_status (user_id, revoked_at, expires_at),
  CONSTRAINT fk_sessions_user FOREIGN KEY (user_id)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 治理：幂等键
--
-- 与早期版本的差别：不存 response_json。只记 resource_type / resource_id，
-- 回放时从业务表读实际数据。这样既省掉 200 KB 的 JSON 列，也消除了
-- "幂等回放的数据与业务表不一致"的可能。
-- 预留、业务写入、审计、completed 标记都在**同一个事务**内完成。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idempotency_keys (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id       BIGINT UNSIGNED NOT NULL,
  capability    VARCHAR(96)     NOT NULL,
  key_hash      CHAR(64)        NOT NULL,
  request_hash  CHAR(64)        NOT NULL,
  status        ENUM('processing','completed','failed') NOT NULL DEFAULT 'processing',
  resource_type VARCHAR(64)     NULL,
  resource_id   BIGINT UNSIGNED NULL,
  result_code   VARCHAR(64)     NULL,
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at    DATETIME        NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_idempotency_scope (user_id, capability, key_hash),
  KEY idx_idempotency_expiry (expires_at),
  CONSTRAINT fk_idempotency_user FOREIGN KEY (user_id)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 治理：人工确认（"先准备、后确认、再执行"）
--
-- 绑定六元组：user / session / capability / params_hash / target / expires。
-- params_hash 覆盖执行时的实际参数——确认卡片上给用户看的"填写内容"
-- 必须与 Execute 时提交的参数逐字节一致，否则拒绝。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_confirmations (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  token_hash     CHAR(64)        NOT NULL,
  user_id        BIGINT UNSIGNED NOT NULL,
  session_hash   CHAR(64)        NOT NULL,
  conversation_id VARCHAR(64)    NOT NULL,
  capability     VARCHAR(96)     NOT NULL,
  params_hash    CHAR(64)        NOT NULL,
  payload_json   JSON            NOT NULL,
  target_ref     VARCHAR(255)    NOT NULL DEFAULT '',
  summary        VARCHAR(255)    NOT NULL DEFAULT '',
  status         ENUM('pending','confirmed','cancelled','expired','failed') NOT NULL DEFAULT 'pending',
  idempotency_key VARCHAR(128)   NULL,
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at     DATETIME        NOT NULL,
  used_at        DATETIME        NULL,
  failure_reason VARCHAR(255)    NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_agent_confirmations_token (token_hash),
  KEY idx_agent_confirmations_pending (user_id, status, expires_at),
  CONSTRAINT fk_agent_confirmations_user FOREIGN KEY (user_id)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 审计：append-only，在数据库层强制
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_logs (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  request_id   VARCHAR(64)     NOT NULL DEFAULT '',
  user_id      BIGINT UNSIGNED NULL,
  username     VARCHAR(64)     NOT NULL DEFAULT '',
  -- Agent 相关字段：能回答"谁通过 AI 在什么时候执行了什么业务"
  is_agent     TINYINT(1)      NOT NULL DEFAULT 0,
  conversation_id VARCHAR(64)  NULL,
  confirmation_id BIGINT UNSIGNED NULL,
  capability   VARCHAR(96)     NOT NULL,
  domain       VARCHAR(64)     NOT NULL,
  permission   VARCHAR(96)     NOT NULL DEFAULT '',
  object_type  VARCHAR(64)     NOT NULL DEFAULT '',
  object_id    BIGINT UNSIGNED NULL,
  object_ref   VARCHAR(255)    NOT NULL DEFAULT '',
  result       ENUM('success','failure','denied','pending') NOT NULL,
  reason       VARCHAR(255)    NOT NULL DEFAULT '',
  before_json  JSON            NULL,
  after_json   JSON            NULL,
  detail_json  JSON            NULL,
  ip_address   VARCHAR(45)     NOT NULL DEFAULT '',
  created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_audit_user_created (user_id, created_at),
  KEY idx_audit_capability_created (capability, created_at),
  KEY idx_audit_request (request_id),
  KEY idx_audit_agent (is_agent, created_at),
  KEY idx_audit_object (object_type, object_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 继承早期版本已验证的做法：在数据库层禁止改写审计

DELIMITER $$
CREATE TRIGGER audit_logs_no_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'$$

CREATE TRIGGER audit_logs_no_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'$$
DELIMITER ;

-- ---------------------------------------------------------------------------
-- 业务版本快照：每次写入留痕，与能力声明的 audit.before_after 配合
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS record_revisions (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  entity_type   VARCHAR(64)     NOT NULL,
  entity_id     BIGINT UNSIGNED NOT NULL,
  version_no    INT UNSIGNED    NOT NULL,
  before_json   JSON            NULL,
  after_json    JSON            NULL,
  actor_user_id BIGINT UNSIGNED NOT NULL,
  is_agent      TINYINT(1)      NOT NULL DEFAULT 0,
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_record_revisions_entity_version (entity_type, entity_id, version_no),
  KEY idx_record_revisions_actor_created (actor_user_id, created_at),
  CONSTRAINT fk_record_revisions_actor FOREIGN KEY (actor_user_id)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 登录限流（早期版本把这套逻辑写在 rate_limit_repository 的 SQL 里，
-- 而同名模块 rate_limiter.py 整个 30 行零调用 —— 死代码）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rate_limits (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  bucket       VARCHAR(128)    NOT NULL,
  hits         INT UNSIGNED    NOT NULL DEFAULT 0,
  window_start DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_rate_limits_bucket (bucket),
  KEY idx_rate_limits_window (window_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
