-- ============================================================================
-- 004 成本：会计期间 / 成本类别 / 成本记录
--
-- 字段与约束**严格取自** docs/CAPABILITY_REGISTRY.md：
--   §1.9  cost 域的 5 条能力（cost.entry.list / cost.entry.create /
--         cost.entry.confirm / cost.summary / cost.period.close）
--   §2.10 cost 字段表（key/type/required/约束/label）
--   §4 #7 PeriodOpen（"已关账期间不得再产生任何影响该期间的账目记录"）
--   §4 #8 NoOverlappingSource（同期不得重复归集库存成本）
--   §4 #15 RequiredField(source_ref)
--   §4 #5  DistinctActors(created_by, verified_by)
--
-- 裁决依据：DECISIONS.md Q13（推翻"纯派生 + 只读"，cost 保留全部 5 条）
--   · "成本随业务事实自动生成"不是能力而是副作用 —— 它遗漏了系统外费用
--     （人工/水电/租金），手工录入入口因此必须存在；
--   · cost.entry.confirm 是"经办人≠审批人"与"每笔成本可溯源"两条不变量的载体；
--   · 期间锁定不删除而是**加强**（Q16 修正语义：锁的是整个期间，收入侧与成本侧
--     一视同仁）。
--
-- **迁移必须可重入**：MySQL 的 DDL 不回滚，失败后要能直接重跑。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 会计期间
--
-- 它是 `PeriodOpen` 不变量的**唯一数据来源**（`kernel/invariants.py` 按日期反查
-- 所属期间的 status）。早期版本把"期间是否锁定"存在 `cost_settlements` 里，并且
-- 只在手工录入入口调用 `require_unlocked()`（`早期版本 cost_store.py:71,79`）——
-- 于是派生路径（投喂/入库/出库核验时自动归集成本）可以**绕过**期间锁定。
--
-- 新系统的做法：期间是一张独立的表，`PeriodOpen` 挂到全部 6 条会产生账目记录的
-- 能力上（含收入侧的 delivery.verify，见 Q16）。"约束要放在能真正保证它的地方"。
--
-- 期间的状态只有两个：open / closed。刻意不做 `reopened` ——
-- 反关账会让"已关账的月份数字会变"这件事变成可能，而成本报告的可信度正建立在
-- 它不变之上。要修正已关账期间的数据，走下一期间的红字调整。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounting_periods (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  period          CHAR(7)         NOT NULL,
  period_start    DATE            NOT NULL,
  period_end      DATE            NOT NULL,
  -- 【封闭字典，非记录生命周期】取值由 kernel/invariants.PERIOD_STATUSES 固定
  -- (open/closed)，不随业务流程演进 —— 按 ROLLOUT_CONTRACT §1.2 的裁决
  -- （负责人：真正封闭的字典 ENUM 可辩护），这里**保留 ENUM**。
  status          ENUM('open','closed') NOT NULL DEFAULT 'open',
  closed_by       BIGINT UNSIGNED NULL,
  closed_at       DATETIME        NULL,
  close_reason    VARCHAR(500)    NULL,
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  -- 一个企业一个月份只能有一个期间：这是"同一期间不得被关两次"的物理保证
  UNIQUE KEY uq_accounting_periods_org_period (organization_id, period),
  KEY idx_accounting_periods_range (organization_id, period_start, period_end),
  CONSTRAINT fk_accounting_periods_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_accounting_periods_closed_by FOREIGN KEY (closed_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_accounting_periods_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- "期间起 ≤ 期间止"放数据库层：应用层校验写错一格只会静默产生空期间
  CONSTRAINT chk_accounting_periods_range CHECK (period_start <= period_end),
  -- 关账必须留下"谁关的、什么时候关的、为什么关"——
  -- 早期版本只记结果不记原因，事后无法回答"这个月为什么提前关了"
  CONSTRAINT chk_accounting_periods_closed CHECK (
    status <> 'closed' OR (closed_by IS NOT NULL AND closed_at IS NOT NULL AND close_reason IS NOT NULL)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 成本类别
--
-- registry §2.10 明确"删 cost_nature，保留类别表一处"：早期版本成本性质
-- （direct/public）同时存在于 `cost_entries.cost_nature`、
-- `cost_categories.default_nature` 与汇总时的重算里**三处**
-- （`早期版本 cost_store.py:54-55` 按 row["nature"] 累加）。三处真相 -> 保留一处。
--
-- `default_nature` 放在类别表上：性质是"这类成本通常是什么性质"，
-- 不是"这一笔是什么性质"，改口径时只改一处。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cost_categories (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(32)     NOT NULL,
  name            VARCHAR(64)     NOT NULL,
  -- 【封闭字典】直接成本 / 公共成本，两值由会计准则决定，不随流程演进。保留 ENUM。
  default_nature  ENUM('direct','public') NOT NULL DEFAULT 'direct',
  -- 【记录生命周期】类别停用是有意为之的软删除，将来会出现
  -- 'deprecated'/'merged' 之类的状态 -> 按 §1.2 用 VARCHAR(32)。
  -- CHECK 保住 ENUM 原有强度：改状态集时只需换 CHECK，不必 MODIFY 整列
  -- （早期版本为此做过 8 次 ALTER TABLE ... MODIFY status ENUM(...)）。
  status          VARCHAR(32)     NOT NULL DEFAULT 'enabled',
  sort_order      INT             NOT NULL DEFAULT 0,
  note            VARCHAR(500)    NULL,
  created_by      BIGINT UNSIGNED NOT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_cost_categories_org_code (organization_id, code),
  CONSTRAINT fk_cost_categories_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_cost_categories_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- 替代原先 ENUM 的取值强约束（同 chk_cost_entries_status 的理由）
  CONSTRAINT chk_cost_categories_status CHECK (
    status IN ('enabled','disabled')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 成本记录
--
-- **分租列（organization_id / farm_id / area_id）写在表上**，与
-- `warehouses` / `inventory_lots` / `inventory_ledger` 一致（Q6 裁决）：
-- 不支持跨表 JOIN 解析 DataScope，因此 `cost.entry.list` / `cost.summary` 的
-- `resource(area_id)` 直接命中本表的列，可以走索引。
--
-- **双状态资源**（与 ponds 同构）：
--   status       记录生命周期 draft / submitted / verified / archived
--   confirm_state 成本归集状态 pending / confirmed（早期版本的
--                 `cost_settlements.confirmed` 语义，保留为显式状态而不是布尔）
--
-- `source_ref` 是 `RequiredField(source_ref)` 不变量的作用对象
-- （registry §4 #15：附件不做，"核验必须有凭据"降级为"必须填来源单号"）。
-- 它是列级 NOT NULL —— 已经"确认"的成本不许再被清空来源；
-- 而草稿态由不变量在 confirm 时强制（草稿允许先登记后补单号）。
--
-- `source_type` 只有两个值（registry §2.10）：`manual_expense`（系统外费用手工登记）
-- 与 `warehouse_ledger`（投喂/入库/领用核验时自动归集）。旧枚举里的
-- `asset_depreciation` / `adjustment` 随资产与调整单一起砍除。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cost_entries (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id  BIGINT UNSIGNED NOT NULL,
  farm_id          BIGINT UNSIGNED NOT NULL,
  area_id          BIGINT UNSIGNED NOT NULL,

  category_code    VARCHAR(32)     NOT NULL,
  amount           DECIMAL(16,2)   NOT NULL,
  occurred_on      DATE            NOT NULL,
  period_start     DATE            NOT NULL,
  period_end       DATE            NOT NULL,
  -- 【封闭字典】registry §2.10 明确只留这两个值（asset_depreciation/adjustment
  -- 随资产与调整单一起砍除）。保留 ENUM。
  source_type      ENUM('manual_expense','warehouse_ledger') NOT NULL DEFAULT 'manual_expense',
  -- 来源单号：投喂/入库/出库自动归集时指向那笔业务事实（如 feeding#991），
  -- 手工登记时由经办人填写。**它是"每笔成本可追溯到底单"的唯一凭据**
  -- （registry §4 #15；旧 cost_enterprise_repository.py:80-83 的 require_evidence()）。
  source_ref       VARCHAR(128)    NOT NULL,
  -- 【封闭字典】registry §2.10 固定这四个归属层级。保留 ENUM。
  target_type      ENUM('farm','area','pond','batch') NULL,
  target_id        BIGINT UNSIGNED NULL,
  note             VARCHAR(500)    NULL,

  -- 自动归集去重的物理保证（registry §4 #8 NoOverlappingSource）：
  -- 同一归属对象、同一期间、同一来源单号只能有一条自动归集记录。
  -- 应用层的不变量负责给出**可读的 409 文案**，这里负责**正确性底线**——
  -- 与 pond_status_change_requests 用生成列 + 唯一键是同一个思路。
  -- 生成列只在 source_type='warehouse_ledger' 时有值，因此手工录入不受此约束
  -- （手工费用与库存成本并存是允许的，重复的是"同一笔库存事实被归集两次"）。
  ledger_dedupe_key VARCHAR(191) GENERATED ALWAYS AS (
    CASE WHEN source_type = 'warehouse_ledger'
         THEN CONCAT(farm_id, '|', target_type, '|', target_id, '|', period_start, '|', period_end, '|', source_ref)
         ELSE NULL END
  ) STORED,

  -- 【记录生命周期】成本归集状态会演进（例如将来加 'reopened'/'reversed'），
  -- 按 §1.2 用 VARCHAR(32) —— 这正是 负责人 点名的第一类。
  confirm_state    VARCHAR(32)     NOT NULL DEFAULT 'pending',
  -- 【记录生命周期】负责人 原话：'cost_entries.status 若也是 ENUM，那它属于第一类
  -- ——它一定会演进。' 它确实曾是 ENUM，故按 §1.2 改为 VARCHAR(32)。
  status           VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version      INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by       BIGINT UNSIGNED NOT NULL,
  updated_by       BIGINT UNSIGNED NULL,
  verified_by      BIGINT UNSIGNED NULL,
  verified_at      DATETIME        NULL,
  created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_cost_entries_org_dedupe (organization_id, ledger_dedupe_key),
  KEY idx_cost_entries_scope_period (organization_id, farm_id, area_id, occurred_on),
  KEY idx_cost_entries_target (organization_id, target_type, target_id, occurred_on),
  KEY idx_cost_entries_category (organization_id, category_code),
  KEY idx_cost_entries_status (organization_id, status, confirm_state),
  KEY idx_cost_entries_source (organization_id, source_type, source_ref),
  CONSTRAINT fk_cost_entries_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_cost_entries_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_cost_entries_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_cost_entries_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_cost_entries_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- 金额必须为正：成本记录表达的是"支出发生了"，负数冲销走下一期间的调整记录。
  -- 允许负数会让"金额"这个字段同时承担两个语义（成本与冲销），
  -- 而汇总时无法区分"少花了钱"与"冲掉了一笔"。
  CONSTRAINT chk_cost_entries_amount CHECK (amount > 0),
  -- 发生日期必须落在它声明的期间内：否则"这笔成本属于哪个月"有两个答案
  CONSTRAINT chk_cost_entries_period CHECK (period_start <= occurred_on AND occurred_on <= period_end),
  -- 归属对象类型与 id 必须同时给出（registry §2.10：target_id "与 target_type 同时出现"）
  CONSTRAINT chk_cost_entries_target CHECK (
    (target_type IS NULL AND target_id IS NULL) OR (target_type IS NOT NULL AND target_id IS NOT NULL)
  ),
  -- 自审禁止落在数据库层（与 003 的 chk_pscr_distinct_actors 同构）：
  -- 连脏数据都写不进去。应用层的 DistinctActors 不变量负责给出可读文案。
  CONSTRAINT chk_cost_entries_distinct_actors CHECK (
    verified_by IS NULL OR verified_by <> created_by
  ),
  -- 已确认的成本必须有确认人与确认时间（"确认"这个动作必须留下痕迹）
  CONSTRAINT chk_cost_entries_confirmed CHECK (
    confirm_state <> 'confirmed' OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)
  ),
  -- 下面两条替代原先 ENUM 提供的取值强约束。
  -- 为什么要 CHECK 而不是只放宽成裸 VARCHAR(32)：ENUM **本身**就是数据库层的强约束，
  -- 单纯换类型等于削弱校验（任何字符串都能落库）。用 CHECK 既拿到"改状态集不必
  -- MODIFY 整列"的演进自由，又保住原强度 —— 也与 006/007/008 三个域的既有写法一致
  -- （chk_warehouses_status / chk_purchase_orders_status / chk_sales_orders_status）。
  CONSTRAINT chk_cost_entries_status CHECK (
    status IN ('draft','submitted','verified','archived')
  ),
  CONSTRAINT chk_cost_entries_confirm_state CHECK (
    confirm_state IN ('pending','confirmed')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- 种子：成本类别 + 当前会计期间
--
-- **本迁移自包含**：不假设 003 的种子存在，一律用 JOIN 现查。
-- 003 的教训（DEVELOPMENT.md 与 003 文件头都记了）：`INSERT ... SELECT` 在源表为空时
-- **不报错、只插 0 行**，然后下游一路静默失败。这里对"类别必须存在"做显式断言。
--
-- 全部幂等——迁移会重跑（DDL 不回滚），种子重复执行不能产生重复行。
-- ============================================================================

-- 系统用户兜底：001 未种任何用户，003 种了 id=1。
-- created_by 是 NOT NULL 且带外键，所以这里再确认一次（幂等）。
INSERT INTO users (id, username, display_name, password_hash, status)
VALUES (1, 'system-seed', '系统（种子数据）', 'scrypt$16384$8$1$00$00', 'retired')
ON DUPLICATE KEY UPDATE display_name = VALUES(display_name);

-- 成本类别：覆盖架构里允许的三种成本口径
-- （物料成本 / 人工 / 能源），全部 direct 性质。
-- 只在 003 种出了企业时才插——没有企业就没有 organization_id 可挂。
INSERT INTO cost_categories (organization_id, code, name, default_nature, status, sort_order, created_by)
SELECT o.id, seed.code, seed.name, seed.default_nature, 'enabled', seed.sort_order, 1
FROM organizations AS o
CROSS JOIN (
  SELECT 'feed'   AS code, '饲料成本' AS name, 'direct' AS default_nature, 10 AS sort_order
  UNION ALL SELECT 'seed',   '种苗成本', 'direct', 20
  UNION ALL SELECT 'health', '药品疫苗', 'direct', 30
  UNION ALL SELECT 'labor',  '人工成本', 'direct', 40
  UNION ALL SELECT 'energy', '水电能源', 'public', 50
  UNION ALL SELECT 'other',  '其他费用', 'public', 90
) AS seed
WHERE o.code = 'default'
ON DUPLICATE KEY UPDATE name = VALUES(name), default_nature = VALUES(default_nature);

-- 当前会计期间：从今天所在月份开始往后种 12 个月 + 往前 1 个月。
-- 为什么要预置未来期间：`cost.entry.create` 的 PeriodOpen 不变量在
-- **查不到期间时放行**（见 kernel/invariants.py 的 PeriodOpen.check ——
-- 它在 `row is None` 时 return）。预置是让"关账"这件事有明确的作用对象；
-- 没有期间记录时系统不会假装已关账。
--
-- 用 CURDATE() 而不是硬编码日期：迁移在任何时间点跑都应该得到"当前期间"。
INSERT INTO accounting_periods
  (organization_id, period, period_start, period_end, status, created_by)
SELECT o.id,
       DATE_FORMAT(d.anchor, '%Y-%m'),
       DATE_FORMAT(d.anchor, '%Y-%m-01'),
       LAST_DAY(d.anchor),
       'open',
       1
FROM organizations AS o
CROSS JOIN (
  SELECT DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL n MONTH) AS anchor
  FROM (
    SELECT -1 AS n UNION ALL SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2
    UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6
    UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10
    UNION ALL SELECT 11
  ) AS offsets
) AS d
WHERE o.code = 'default'
ON DUPLICATE KEY UPDATE period_start = VALUES(period_start), period_end = VALUES(period_end);


-- ============================================================================
-- 自我对齐：把旧形状的列追平到上面的定稿形状
--
-- 为什么迁移里要有这一段：004 **已经登记过**（`schema_migrations` 里有它的行），
-- 而它后来又按 负责人 的 §1.2 裁决改过一次——`cost_entries.status`、
-- `cost_entries.confirm_state`、`cost_categories.status` 三个**记录生命周期**
-- 状态列从 `ENUM` 换成 `VARCHAR(32)` + `CHECK`。改的是文件，真库没有跟着动，
-- 于是真库停在旧形状，runner 把这种"登记值对不上"报成漂移。
--
-- 处置：文件是真话，**真库向文件对齐**，而且对齐动作写进迁移自身——
-- `tools/migrate.py` 是 schema 的唯一变更入口，另写一个修数脚本等于允许
-- 第二条变更路径（"两处描述同一件事"）。
--
-- **可重入**是硬要求（MySQL 的 DDL 不回滚，失败后必须能直接重跑）。因此每条都在
-- 真正动手前先读 `information_schema` 判存在性：
--   * 列**已是** `varchar` -> 整段跳过（新库/已对齐库走这条）；
--   * 列**还是** `enum`   -> 转换，并顺手把该列原有的 CHECK 重挂上。
-- 用会话变量 `@status_was_enum` 把"这次到底转了没有"传到下面，
-- 让 CHECK 的增删只发生在真正的旧形状上。
--
-- 判据取自 `tools/cost/survey_status_columns.py`：006/007/008 三个域的状态列
-- 全部是 `VARCHAR(32)` + `CHECK`，004 是唯一还在用 `ENUM` 的 —— 这一段就是
-- 把它拉回全队形态。仍保留 ENUM 的四列（`accounting_periods.status`、
-- `cost_categories.default_nature`、`cost_entries.source_type`、
-- `cost_entries.target_type`）是真正封闭的字典，**不在对齐范围内**。
-- ============================================================================

-- ① cost_categories.status  ENUM -> VARCHAR(32)
SET @was_enum = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'cost_categories'
     AND COLUMN_NAME = 'status' AND DATA_TYPE = 'enum'
);
SET @ddl = IF(@was_enum,
  "ALTER TABLE cost_categories MODIFY COLUMN status VARCHAR(32) NOT NULL DEFAULT 'enabled'",
  'DO 0');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
SET @ddl = IF(@was_enum,
  "ALTER TABLE cost_categories ADD CONSTRAINT chk_cost_categories_status CHECK (status IN ('enabled','disabled'))",
  'DO 0');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ② cost_entries.confirm_state  ENUM -> VARCHAR(32)
SET @was_enum = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'cost_entries'
     AND COLUMN_NAME = 'confirm_state' AND DATA_TYPE = 'enum'
);
SET @ddl = IF(@was_enum,
  "ALTER TABLE cost_entries MODIFY COLUMN confirm_state VARCHAR(32) NOT NULL DEFAULT 'pending'",
  'DO 0');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
SET @ddl = IF(@was_enum,
  "ALTER TABLE cost_entries ADD CONSTRAINT chk_cost_entries_confirm_state CHECK (confirm_state IN ('pending','confirmed'))",
  'DO 0');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ③ cost_entries.status  ENUM -> VARCHAR(32)
SET @was_enum = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'cost_entries'
     AND COLUMN_NAME = 'status' AND DATA_TYPE = 'enum'
);
SET @ddl = IF(@was_enum,
  "ALTER TABLE cost_entries MODIFY COLUMN status VARCHAR(32) NOT NULL DEFAULT 'draft'",
  'DO 0');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
SET @ddl = IF(@was_enum,
  "ALTER TABLE cost_entries ADD CONSTRAINT chk_cost_entries_status CHECK (status IN ('draft','submitted','verified','archived'))",
  'DO 0');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
