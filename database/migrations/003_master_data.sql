-- ============================================================================
-- 003 主数据：区域 / 塘口 / 塘口状态变更申请 / 往来单位 / 物料
--
-- 字段与约束**严格取自** docs/CAPABILITY_REGISTRY.md：
--   §2.5  master_data 字段表（key/type/required/约束/label）
--   §3.1  pond 状态机（status 生命周期 + pond_status 业务状态 + 转移表）
--
-- 不凭记忆写：registry 是权威清单，且它每个字面值都可追溯到早期版本的生产验证结果。
--
-- **迁移必须可重入**：MySQL 的 DDL 不回滚，失败后要能直接重跑。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 区域
--
-- 它同时承担两个角色：塘口的必填归属，以及 DataScope 的挂载点
-- （data_scopes.area_id 指它）。这是早期版本的设计，继承——
-- "数据范围按区域划分"是真实的业务约束，不是技术选择。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS areas (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(120)    NOT NULL,
  status          ENUM('draft','submitted','verified','archived') NOT NULL DEFAULT 'draft',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_areas_farm_code (farm_id, code),
  KEY idx_areas_scope_status (organization_id, farm_id, status),
  CONSTRAINT fk_areas_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_areas_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_areas_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 塘口
--
-- **双状态资源**（registry §3.1-A/B）：
--   status      记录生命周期（draft / submitted / verified / archived）
--   pond_status 业务状态（build / stocked / farming / rest / clean / rebuild）
--
-- pond_status 只能经两步审批变更（pond_status_change.request +
-- pond_status_change.verify，Q2 裁决）。"pond.update 拒绝提交此字段"这条
-- 由**服务层**强制——数据库表达不了"是谁在改"。
--
-- **刻意不冗余存塘汇总**：registry §2.5 明确删掉了早期版本的
-- current_spec / stock_quantity / stock_quantity_source。早期版本自己在
-- 旧 product/production/routes.py:100-101 的注释里承认了这个冗余。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ponds (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  area_id         BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(100)    NOT NULL,
  species         VARCHAR(64)     NULL,
  capacity_mu     DECIMAL(12,2)   NULL,
  pond_status     ENUM('build','stocked','farming','rest','clean','rebuild')
                    NOT NULL DEFAULT 'build',
  manager_name    VARCHAR(40)     NULL,
  location_text   VARCHAR(200)    NULL,
  aerator_count   INT UNSIGNED    NULL,
  stocking_spec   VARCHAR(64)     NULL,
  note            VARCHAR(500)    NULL,

  status          ENUM('draft','submitted','verified','archived') NOT NULL DEFAULT 'draft',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  updated_by      BIGINT UNSIGNED NULL,
  verified_by     BIGINT UNSIGNED NULL,
  verified_at     DATETIME        NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_ponds_farm_code (farm_id, code),
  KEY idx_ponds_scope_status (organization_id, farm_id, area_id, status),
  KEY idx_ponds_pond_status (pond_status),
  CONSTRAINT fk_ponds_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_ponds_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_ponds_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_ponds_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_ponds_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  -- 容量上限继承旧 master_data_service.py:26（MAX_CAPACITY_MU）
  CONSTRAINT chk_ponds_capacity CHECK (
    capacity_mu IS NULL OR (capacity_mu > 0 AND capacity_mu <= 100000)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 塘口状态变更申请（两步审批的载体）
--
-- 关键设计：active_pond_id 是**生成列**，只在 status='submitted' 时有值，
-- 配唯一键实现"**同一塘口只能有一个待核验申请**"。
--
-- 为什么用生成列 + 唯一键，而不是应用层"先查再插"：
-- 后者在并发下两个请求会**都通过检查**再都插入；唯一键是数据库级的保证。
-- **约束要放在能真正保证它的地方。**
--
-- 这正是 registry §3.1 说的"证明系统能表达集合上唯一，而不只是字段非空"。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pond_status_change_requests (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  pond_id         BIGINT UNSIGNED NOT NULL,
  from_status     ENUM('build','stocked','farming','rest','clean','rebuild') NOT NULL,
  to_status       ENUM('build','stocked','farming','rest','clean','rebuild') NOT NULL,
  reason          VARCHAR(500)    NOT NULL,
  status          ENUM('submitted','verified','cancelled') NOT NULL DEFAULT 'submitted',
  -- 申请时的塘口版本：核验时比对，防"申请期间塘口被别的操作改过"
  pond_version    INT UNSIGNED    NOT NULL,
  requested_by    BIGINT UNSIGNED NOT NULL,
  requested_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  verified_by     BIGINT UNSIGNED NULL,
  verified_at     DATETIME        NULL,
  active_pond_id  BIGINT UNSIGNED GENERATED ALWAYS AS
                    (CASE WHEN status = 'submitted' THEN pond_id ELSE NULL END) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY uq_pond_status_active_request (active_pond_id),
  KEY idx_pond_status_history (pond_id, requested_at),
  CONSTRAINT fk_pscr_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_pscr_pond FOREIGN KEY (pond_id)
    REFERENCES ponds(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_pscr_requested_by FOREIGN KEY (requested_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_pscr_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- 自审禁止落在数据库层：连脏数据都写不进去
  CONSTRAINT chk_pscr_distinct_actors CHECK (
    verified_by IS NULL OR verified_by <> requested_by
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 往来单位（供应商 / 客户合并）
--
-- registry §2.5 的裁决依据：早期版本 master_data_service.py:17-18 里
-- suppliers 与 customers 的字段集**完全相同**，合并是去重而非发明。
--
-- partner_type 是**新增的显式字段**：早期版本靠 SPECS 映射把资源名翻成
-- partner_type 再 WHERE partner_type=%s 过滤（旧 master_data_store.py:22-23,50-52）。
-- 类型是隐藏的；新系统让它可见、可查、可约束。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS business_partners (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id  BIGINT UNSIGNED NOT NULL,
  farm_id          BIGINT UNSIGNED NOT NULL,
  area_id          BIGINT UNSIGNED NOT NULL,
  partner_type     ENUM('supplier','customer') NOT NULL,
  code             VARCHAR(64)     NOT NULL,
  name             VARCHAR(100)    NOT NULL,
  contact_name     VARCHAR(40)     NULL,
  phone            VARCHAR(32)     NULL,
  address          VARCHAR(200)    NULL,
  settlement_days  INT UNSIGNED    NULL,
  credit_limit     DECIMAL(14,2)   NULL,
  note             VARCHAR(500)    NULL,
  status           ENUM('draft','submitted','verified','archived') NOT NULL DEFAULT 'draft',
  row_version      INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by       BIGINT UNSIGNED NOT NULL,
  created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  -- 唯一性限定在"企业 + 类型"内：同一编号下供应商与客户可以并存
  UNIQUE KEY uq_partners_org_type_code (organization_id, partner_type, code),
  KEY idx_partners_scope_type (organization_id, farm_id, partner_type, status),
  CONSTRAINT fk_partners_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_partners_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_partners_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_partners_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 物料
--
-- category 用 VARCHAR 而非 ENUM：饲料/药品/物资的划分会随业务演进，
-- 而 ENUM 每加一个值都要动 DDL。**会变的分类不该固化成约束。**
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS materials (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  area_id         BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(120)    NOT NULL,
  category        VARCHAR(32)     NOT NULL DEFAULT 'feed',
  spec            VARCHAR(64)     NULL,
  unit            VARCHAR(16)     NOT NULL DEFAULT 'kg',
  unit_price      DECIMAL(14,4)   NULL,
  safety_stock    DECIMAL(16,3)   NULL,
  shelf_life_days INT UNSIGNED    NULL,
  note            VARCHAR(500)    NULL,
  status          ENUM('draft','submitted','verified','archived') NOT NULL DEFAULT 'draft',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_materials_org_code (organization_id, code),
  KEY idx_materials_scope_status (organization_id, farm_id, status),
  CONSTRAINT fk_materials_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_materials_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_materials_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_materials_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- 种子：一份可用的最小数据面
--
-- **本迁移自包含**：先保证企业/基地存在，再种区域。
--
-- 为什么不假设 `organizations` / `farms` 已有数据：它们由 001 建表，
-- 但那份迁移只建表不种数据（早期版本的对应种子在 006，我没有带过来）。
-- 于是初版的 `INSERT INTO areas ... SELECT ... FROM farms` 匹配 0 行，
-- **静默插入 0 条**——不报错，只是没有数据，然后下游一路失败：
-- 区域 0 行 -> 塘口 select 不到 area -> 塘口 0 行。
--
-- `INSERT ... SELECT` 在源表为空时不报错，只插 0 行。这个形态很值得记：
-- **迁移应当自包含**——给定一个空库，能一路跑到可用状态。
-- ============================================================================

-- 系统用户：供种子行的责任人字段引用。
--
-- 存在的理由：`created_by` 是 NOT NULL 且带外键，而"经办人 ≠ 审批人"
-- 这条合规规则要求**每个动作都有明确责任人**——所以不能把它设为 NULL 绕过。
--
-- 固定 id=1：让种子可以稳定引用。它不可登录——`password_hash` 是一个
-- **不可能匹配的哨兵值**（格式合法但 `verify_password` 永远返回 False），
-- 状态也是 retired。它只能当责任人占位，不能作为入口。
INSERT INTO users (id, username, display_name, password_hash, status)
VALUES (1, 'system-seed', '系统（种子数据）', 'scrypt$16384$8$1$00$00', 'retired')
ON DUPLICATE KEY UPDATE display_name = VALUES(display_name);

INSERT INTO organizations (code, name, status)
VALUES ('default', '默认企业', 'active')
ON DUPLICATE KEY UPDATE name = VALUES(name);

INSERT INTO farms (organization_id, code, name, status)
SELECT o.id, 'default-farm', '默认基地', 'active'
FROM organizations AS o
WHERE o.code = 'default'
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 理由：前端 ResourceListPage 的 ref 字段（如 area_id）需要候选值，
-- 而"一个区域都没有"会让塘口根本建不出来。给最小可用数据，clone 下来就能跑主流程。
--
-- 全部幂等——迁移会重跑（DDL 不回滚），种子重复执行不能产生重复行。
-- ============================================================================

INSERT INTO areas (organization_id, farm_id, code, name, status, created_by)
SELECT f.organization_id, f.id, seed.code, seed.name, 'verified', 1
FROM farms AS f
CROSS JOIN (
  SELECT 'A-NORTH' AS code, '北区' AS name
  UNION ALL SELECT 'A-SOUTH', '南区'
) AS seed
WHERE f.code = 'default-farm'
ON DUPLICATE KEY UPDATE name = VALUES(name);

INSERT INTO business_partners
  (organization_id, farm_id, area_id, partner_type, code, name,
   contact_name, phone, settlement_days, status, created_by)
SELECT a.organization_id, a.farm_id, a.id, seed.partner_type, seed.code, seed.name,
       seed.contact_name, seed.phone, seed.settlement_days, 'verified', 1
FROM areas AS a
CROSS JOIN (
  SELECT 'supplier' AS partner_type, 'SUP-001' AS code, '示范饲料供应商' AS name,
         '张供应' AS contact_name, '13800000001' AS phone, 30 AS settlement_days
  UNION ALL
  SELECT 'customer', 'CUS-001', '示范水产采购商', '李采购', '13800000002', 15
) AS seed
WHERE a.code = 'A-NORTH'
ON DUPLICATE KEY UPDATE name = VALUES(name);

INSERT INTO materials
  (organization_id, farm_id, area_id, code, name, category, spec, unit,
   unit_price, safety_stock, shelf_life_days, status, created_by)
SELECT a.organization_id, a.farm_id, a.id, seed.code, seed.name, seed.category,
       seed.spec, 'kg', seed.unit_price, seed.safety_stock, seed.shelf_life_days,
       'verified', 1
FROM areas AS a
CROSS JOIN (
  SELECT 'MAT-001' AS code, '1号虾配合饲料' AS name, 'feed' AS category,
         '1.0mm 颗粒' AS spec, 4.8000 AS unit_price,
         500.000 AS safety_stock, 180 AS shelf_life_days
  UNION ALL
  SELECT 'MAT-002', '2号虾配合饲料', 'feed', '1.5mm 颗粒', 4.5000, 500.000, 180
) AS seed
WHERE a.code = 'A-NORTH'
ON DUPLICATE KEY UPDATE name = VALUES(name);
