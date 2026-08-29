-- ============================================================================
-- 006 仓储：仓库 / 单据单头 / 单据明细 / 物料批次 / 库存流水
--
-- 字段与约束**严格取自** docs/CAPABILITY_REGISTRY.md：
--   §1.6  warehouse 域的 7 条能力
--   §2.7  warehouse 字段表（receipt.create / issue.create）
--   §3.3  inventory_lots 与 inventory_ledger 两条状态/账本规则
--   §4 #1  不得负库存（issue.verify 是强制点）
--   §4 #3  未审批不得收货（ReferencedStatus）
--   §4 #9  累计到货不得超过采购数量（CumulativeWithin）
--   §4 #19 编码在范围内唯一（uq_warehouse_documents_org_type_code）
--   §7 Q6  分租列直接写在表上，不做跨表 JOIN 解析
--   §7 Q7  create 不接收 organization_id / farm_id / area_id
--
-- ## 本文件的三条硬约束（来自 t12 任务说明与 CAPABILITY_REGISTRY §0.3）
--
-- 1. **可重入**：全部 `CREATE TABLE IF NOT EXISTS`。MySQL 的 DDL 不回滚，
--    失败后必须能直接重跑 `python tools/migrate.py apply`。
-- 2. **不建跨域外键**：引用别的域的表只存 BIGINT（`material_id` / `pond_id` /
--    `batch_id` / `purchase_order_id` / `supplier_id` / `created_by` 之外的业务引用），
--    约束交给不变量（`SameTenant` / `ReferencedStatus`）。这样五个域的迁移顺序
--    不再互相耦合——否则 production / purchase / sales 必须先于 warehouse 建成，
--    而它们的核验路径又都要打这张账本，形成循环依赖。
-- 3. **状态列用 `VARCHAR(32) + CHECK`，不用 ENUM**（registry §3.2 规则 2 /
--    ROLLOUT_CONTRACT §1.2）。初版本文件用了 ENUM，与 005/007/008 的写法不一致：
--    早期版本每加一个状态就要一次 `ALTER TABLE ... MODIFY status ENUM(...)`
--    （实测 8 处），所以"状态枚举不进 DDL"是本仓的硬规则。
--    注意去掉 ENUM **不等于**放弃约束——每个状态列都配了 CHECK 约束，
--    取值约束仍在数据库层，只是改成新增状态无需改表结构即可放宽。
-- 4. **分租列写在表上**：`organization_id` / `farm_id` / `area_id` 三列直接落在
--    `warehouses` / `inventory_lots` / `inventory_ledger` 上，`ScopePolicy.resource("area_id")`
--    因此可以走索引（Q6 裁决：拒绝 `via` 跨表子查询）。
--
-- ## 与早期版本的关键差别
--
-- 早期版本 `warehouse_service.py:11` 有 7 种单据（receipts / issue-requests /
-- issues / returns / transfers / stocktakes / scraps）。本版只保留 receipts 与
-- issues 两种，但**共用一张单头表**（`doc_type` 区分）——早期版本每种单据一张表，
-- 于是"7 种单据各自的编号唯一键、状态机、字段白名单"被写了 7 遍。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 仓库
--
-- `status` 是**记录生命周期**（draft/submitted/verified/archived），与
-- `materials` / `areas` 完全一致（继承，不新造）。
--
-- `is_default` 的存在理由（新增设计）：`feeding.create` 的字段表里
-- **没有 warehouse_id**（registry §2.6 行 703-715），而 `feeding.verify` 必须
-- 扣某个仓的库存。于是"投喂从哪个仓出库"必须有一个服务端可判定的默认值。
-- 做成显式列而不是"取 id 最小的仓"：后者在数据变化后会静默切换到另一个仓，
-- 而库存的去向不该随插入顺序漂移。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS warehouses (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  area_id         BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(100)    NOT NULL,
  address         VARCHAR(200)    NULL,
  contact_name    VARCHAR(40)     NULL,
  phone           VARCHAR(32)     NULL,
  is_default      TINYINT(1)      NOT NULL DEFAULT 0,
  status          VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  verified_by     BIGINT UNSIGNED NULL,
  verified_at     DATETIME        NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_warehouses_farm_code (farm_id, code),
  KEY idx_warehouses_scope_status (organization_id, farm_id, area_id, status),
  CONSTRAINT fk_warehouses_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouses_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouses_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouses_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouses_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT chk_warehouses_status CHECK (
    status IN ('draft','submitted','verified','archived')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 单据单头（receipts 与 issues 共用）
--
-- ## 为什么 receipts 与 issues 共用一张表
--
-- 两者在 registry §2.7 里的字段表**几乎逐字相同**（code/name/warehouse_id/
-- material_id/quantity/happened_at/note），差别只在"入库还是出库"。
-- 早期版本把它们做成两张表，于是字段白名单、编号唯一键、状态机各写一遍。
-- `doc_type` 一个枚举列换掉一整份重复。
--
-- ## 单头 vs 明细的切分（这是 t16 要广播的口径，在此定稿）
--
-- 早期版本是"一单一物料"，所以没有明细表。本版拆出 `warehouse_document_lines`，
-- 因为一单多行是真实需求。拆完之后有一个**必须裁决**的问题：
-- `purchase_order_id` 放单头还是放明细？——这决定 `CumulativeWithin`
-- （registry §4 #9）能不能用**单表 SUM** 表达（它只接受一个 `children_table`，
-- 不能 JOIN）。
--
-- **裁决：放单头。**
-- 理由：到货单的语义是"对某张采购单的一次到货"；采购单一对一到货是 P0 的
-- 真实形态（旧 `purchase_posting.py` 即按单条明细计算）。放单头之后
-- "某采购单累计到货量"就是一条单表聚合：
--
--     SELECT COALESCE(SUM(total_quantity),0) FROM warehouse_documents
--      WHERE purchase_order_id=%s AND doc_type='receipt' AND status='verified'
--
-- **取舍与代价**（必须写下来，否则将来没人知道为什么）：
-- 若某天需要"一张到货单同时对应多张采购单"，单头去规范化就表达不了，
-- 届时要么把 `purchase_order_id` 下沉到明细并改用 join 聚合，
-- 要么约定"一到货单只对一张采购单"。P0 选后者，因为它让不变量保持单表可表达。
--
-- `total_quantity` / `total_amount` 同样是**刻意的去规范化**，理由同上：
-- 不变量要能单表 SUM。它们在核验时由服务端从明细回算并写入，
-- 不接受客户端提交（registry §0.7 规则 1 的同族纪律）。
--
-- `pond_id` / `batch_id`（领用去向）也在单头：一次领用只有一个去向，
-- 与"一张到货单只对一张采购单"是同一种约定。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS warehouse_documents (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id   BIGINT UNSIGNED NOT NULL,
  farm_id           BIGINT UNSIGNED NOT NULL,
  area_id           BIGINT UNSIGNED NOT NULL,
  doc_type          VARCHAR(32)     NOT NULL,
  code              VARCHAR(64)     NOT NULL,
  name              VARCHAR(100)    NOT NULL,
  warehouse_id      BIGINT UNSIGNED NOT NULL,
  material_id       BIGINT UNSIGNED NOT NULL,
  quantity          DECIMAL(18,4)   NOT NULL,
  unit_cost         DECIMAL(14,4)   NULL,
  total_quantity    DECIMAL(18,4)   NOT NULL DEFAULT 0,
  total_amount      DECIMAL(18,4)   NOT NULL DEFAULT 0,
  supplier_id       BIGINT UNSIGNED NULL,
  purchase_order_id BIGINT UNSIGNED NULL,
  pond_id           BIGINT UNSIGNED NULL,
  batch_id          BIGINT UNSIGNED NULL,
  happened_at       DATETIME        NOT NULL,
  note              VARCHAR(500)    NULL,
  status            VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version       INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by        BIGINT UNSIGNED NOT NULL,
  -- 更新者：`*.update` 与状态迁移都会写它。与 `ponds` / `areas` 同构（003 就有这一列），
  -- 继承了"谁改的"这条审计线索。缺它会让写路径的 UPDATE 直接 1054。
  updated_by        BIGINT UNSIGNED NULL,
  verified_by       BIGINT UNSIGNED NULL,
  verified_at       DATETIME        NULL,
  created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  -- registry §4 #19 点名要求的键名与列序
  UNIQUE KEY uq_warehouse_documents_org_type_code (organization_id, doc_type, code),
  KEY idx_warehouse_documents_scope_status (organization_id, farm_id, area_id, doc_type, status),
  KEY idx_warehouse_documents_warehouse (organization_id, warehouse_id, doc_type, status),
  KEY idx_warehouse_documents_receipt_cumulative (organization_id, purchase_order_id, doc_type, status),
  KEY idx_warehouse_documents_happened (organization_id, happened_at),
  CONSTRAINT fk_warehouse_documents_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouse_documents_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouse_documents_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouse_documents_warehouse FOREIGN KEY (warehouse_id)
    REFERENCES warehouses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouse_documents_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouse_documents_updated_by FOREIGN KEY (updated_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_warehouse_documents_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  -- 两张单据的业务形态差异放数据库层，别让应用层自己记得：
  --   到货单（receipt）必须挂供应商；领用单（issue）没有供应商这个概念。
  --   只有到货单能指向采购单——`CumulativeWithin` 若把 issue 行也算进来，
  --   累计到货量会凭空变大。
  CONSTRAINT chk_warehouse_documents_doc_type CHECK (doc_type IN ('receipt','issue')),
  CONSTRAINT chk_warehouse_documents_status CHECK (
    status IN ('draft','submitted','verified','archived')
  ),
  CONSTRAINT chk_warehouse_documents_supplier CHECK (
    (doc_type = 'receipt' AND supplier_id IS NOT NULL AND purchase_order_id IS NULL) IS FALSE
    OR (doc_type = 'receipt')
    OR (doc_type = 'issue' AND supplier_id IS NULL)
  ),
  CONSTRAINT chk_warehouse_documents_quantity CHECK (quantity > 0),
  CONSTRAINT chk_warehouse_documents_unit_cost CHECK (unit_cost IS NULL OR unit_cost >= 0),
  CONSTRAINT chk_warehouse_documents_totals CHECK (total_quantity >= 0 AND total_amount >= 0),
  -- 领用去向：塘口与批次要么都有、要么都没有（只写一个说明填错了）
  CONSTRAINT chk_warehouse_documents_issue_target CHECK (
    (pond_id IS NULL) = (batch_id IS NULL)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 单据明细
--
-- `lot_no` 是**字符串**而不是 `inventory_lot_id`：继承 registry §2.7 的决定——
-- 早期版本允许直接引用已有批次（`早期版本 warehouse_ledger_store.py:26-27`），
-- 新系统统一按 `lot_no` 走幂等建批，于是"批次必须先存在"这个前置条件消失。
-- 服务端在核验时把 `lot_no` 解析成 `inventory_lots.id`（见 ledger.py）。
--
-- `line_no` 从 1 开始，是**账本幂等的行标识**：`inventory_ledger` 的唯一键
-- `uq_inventory_ledger_source_line` 由 `source_ref + source_line_no` 组成，
-- 而 `source_line_no` 就取这里的 `line_no`。两者必须是同一套编号，
-- 否则"同一来源行只追加一次"会变成"同一来源行追加两次都没人拦"。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS warehouse_document_lines (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  document_id    BIGINT UNSIGNED NOT NULL,
  line_no        INT UNSIGNED    NOT NULL,
  material_id    BIGINT UNSIGNED NOT NULL,
  lot_no         VARCHAR(64)     NOT NULL,
  quantity       DECIMAL(18,4)   NOT NULL,
  unit_cost      DECIMAL(14,4)   NULL,
  unit           VARCHAR(16)     NULL,
  production_date DATE           NULL,
  expiry_date    DATE            NULL,
  remark         VARCHAR(200)    NULL,
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_warehouse_document_lines_doc_no (document_id, line_no),
  KEY idx_warehouse_document_lines_material (material_id, lot_no),
  CONSTRAINT fk_warehouse_document_lines_document FOREIGN KEY (document_id)
    REFERENCES warehouse_documents(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT chk_warehouse_document_lines_quantity CHECK (quantity > 0),
  CONSTRAINT chk_warehouse_document_lines_dates CHECK (
    expiry_date IS NULL OR production_date IS NULL OR expiry_date >= production_date
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 物料批次（inventory_lots）
--
-- registry §3.3-A：**本版只保留 `available` / `closed` 两个可设置值**。
-- `quarantined` / `expired` 需要质量管控与到期任务，超出闭环（§6 砍除）；
-- "过期"降级为**查询时计算**，不进状态机。
--
-- **幂等建批**是这张表的关键行为（继承旧 `warehouse_ledger_store.py:30-34`）：
--
--     INSERT INTO inventory_lots (warehouse_id, material_id, lot_no, ...)
--     VALUES (...) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
--
-- `id = LAST_INSERT_ID(id)` 是那句让"插入或取回已有行"用**一条语句**完成、
-- 且并发安全的写法。注意唯一键因此必须包含 `warehouse_id`：
-- 同一个物料批次号可以出现在两个仓里（两个仓各有一本账），
-- 若唯一键只有 (material_id, lot_no)，第二个仓的建批会"取回"第一个仓的行，
-- 于是两个仓**共用同一个批次 id**——账本分组
-- `(warehouse_id, material_id, inventory_lot_id)` 立刻失准。
--
-- 分租列直接落表（Q6）：`inventory.list` / `inventory.ledger` 的能力声明是
-- `resource(area_id)`，直接命中本表列可走索引，不需要 JOIN warehouses。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inventory_lots (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  area_id         BIGINT UNSIGNED NOT NULL,
  warehouse_id    BIGINT UNSIGNED NOT NULL,
  material_id     BIGINT UNSIGNED NOT NULL,
  lot_no          VARCHAR(64)     NOT NULL,
  production_date DATE            NULL,
  expiry_date     DATE            NULL,
  status          VARCHAR(32)     NOT NULL DEFAULT 'available',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_inventory_lots_warehouse_material_lot (warehouse_id, material_id, lot_no),
  KEY idx_inventory_lots_scope_status (organization_id, farm_id, area_id, status),
  -- FEFO（先到期先出）的支撑索引：选批要按 expiry_date 升序，
  -- 且 NULL（无有效期）必须排在最后——没有索引时这是全表扫 + 文件排序。
  KEY idx_inventory_lots_fefo (warehouse_id, material_id, status, expiry_date),
  CONSTRAINT fk_inventory_lots_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_lots_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_lots_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_lots_warehouse FOREIGN KEY (warehouse_id)
    REFERENCES warehouses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_lots_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT chk_inventory_lots_status CHECK (status IN ('available','closed')),
  CONSTRAINT chk_inventory_lots_dates CHECK (
    expiry_date IS NULL OR production_date IS NULL OR expiry_date >= production_date
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 库存流水（inventory_ledger）——**只追加账本**
--
-- registry §3.3-B：无状态。早期版本靠唯一键 `uq_inventory_ledger_source_line`
-- 保证"同一来源单据的同一行只追加一次"，本版**继承该唯一键名**。
--
-- ## 唯一键的三列与 source_ref 的取值约定（跨域契约，t16 广播）
--
-- 唯一键 = `(source_type, source_ref, source_line_no)`。三列的语义：
--
--   source_type      `receipt`（入库）/ `issue`（出库）。registry §3.3 行 1116
--                    只给这两个值（`correction` 明确不做）。**`feeding.verify`
--                    产生的出库也用 `issue`**——它是"生产领用出库"，不是一个新
--                    的单据类型。不新增第三个枚举值，否则 §3.3 的权威表要改，
--                    而"库存流水的来源类型只有两种单据"是本版刻意的收敛。
--
--   source_ref       来源**单据号**（不是自增主键），格式 `<来源域>:<单据号>`：
--                      `warehouse:RCV-20260101-001`（本域 receipt.verify）
--                      `warehouse:ISS-20260101-007`（本域 issue.verify）
--                      `feeding:FEED-20260101-003`   （production 域 feeding.verify）
--                    为什么用单据号而不是主键：自增主键在**不同的单据表里各自
--                    从 1 开始**，`issue#5` 与 `feeding#5` 会撞成同一个
--                    `source_ref`，唯一键就会把两个不同域的正常出库判成重复追加。
--                    加上来源域前缀之后，两个 id 空间不可能相遇。
--                    单据号本身在企业内唯一（`uq_warehouse_documents_org_type_code`
--                    与 `feedings.code` 各自保证），所以这个 ref 是稳定的。
--
--   source_line_no   来源单据的**行号**（1 开始）。单行单据恒为 1。
--
-- 于是"同一来源行只追加一次"是一条**数据库级**保证，而不是应用层的先查后插
-- （后者在并发下两个请求会都通过检查再都插入）。
--
-- ## 为什么分租列要冗余在账本上（而不是 JOIN 仓库表）
--
-- Q6 的裁决：`inventory.ledger` 声明的 scope 是 `resource(area_id)`，
-- 账本表自己带 `area_id` 就能走索引。若改成 JOIN warehouses，就是一个随数据量
-- 增长而劣化的相关子查询——这正是 Q6 推翻初稿的那条理由。
-- 代价是写入时要维护这三列，由服务端从仓库行解析后一并写入（Q7 同源）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inventory_ledger (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id  BIGINT UNSIGNED NOT NULL,
  farm_id          BIGINT UNSIGNED NOT NULL,
  area_id          BIGINT UNSIGNED NOT NULL,
  warehouse_id     BIGINT UNSIGNED NOT NULL,
  material_id      BIGINT UNSIGNED NOT NULL,
  inventory_lot_id BIGINT UNSIGNED NOT NULL,
  lot_no           VARCHAR(64)     NOT NULL,
  source_type      VARCHAR(32)     NOT NULL,
  source_ref       VARCHAR(128)    NOT NULL,
  source_line_no   INT UNSIGNED    NOT NULL,
  quantity_delta   DECIMAL(18,4)   NOT NULL,
  unit_cost        DECIMAL(14,4)   NULL,
  amount           DECIMAL(18,4)   NULL,
  pond_id          BIGINT UNSIGNED NULL,
  batch_id         BIGINT UNSIGNED NULL,
  happened_at      DATETIME        NOT NULL,
  created_by       BIGINT UNSIGNED NOT NULL,
  created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  -- 同一来源单据的同一行只追加一次（继承旧 010_warehouse.sql 的键名）
  UNIQUE KEY uq_inventory_ledger_source_line (source_type, source_ref, source_line_no),
  -- "不得负库存"不变量的余额查询：(warehouse_id, material_id, inventory_lot_id)
  -- 与 §4 #1 的 group_by 逐字一致——分组列的顺序必须和索引前导列一致，
  -- 否则那条 SUM 会退化成全表扫。
  KEY idx_inventory_ledger_balance (warehouse_id, material_id, inventory_lot_id),
  KEY idx_inventory_ledger_scope_time (organization_id, farm_id, area_id, happened_at),
  KEY idx_inventory_ledger_lot_time (inventory_lot_id, happened_at),
  KEY idx_inventory_ledger_material_time (organization_id, material_id, happened_at),
  CONSTRAINT fk_inventory_ledger_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_ledger_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_ledger_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_ledger_warehouse FOREIGN KEY (warehouse_id)
    REFERENCES warehouses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_ledger_lot FOREIGN KEY (inventory_lot_id)
    REFERENCES inventory_lots(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_inventory_ledger_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- 零增量行没有业务含义：它会让"这个批次动过几次"计数失真，
  -- 也会让唯一键占位（同一来源行插入 0 之后再想插入真实增量就被挡住）。
  CONSTRAINT chk_inventory_ledger_source_type CHECK (source_type IN ('receipt','issue')),
  CONSTRAINT chk_inventory_ledger_delta CHECK (quantity_delta <> 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 种子：默认仓库
--
-- 只在 003 已经种出区域时才插（没有区域就没有分租键可挂）。
-- `is_default=1` 让 `feeding.verify` 有确定的出库仓（见上面的列注释）。
-- 沿用 003 的种子形态（SELECT ... FROM areas CROSS JOIN 常量 + ON DUPLICATE KEY）。
-- ---------------------------------------------------------------------------

INSERT INTO warehouses
  (organization_id, farm_id, area_id, code, name, address, contact_name, phone,
   is_default, status, created_by, verified_by, verified_at)
SELECT a.organization_id, a.farm_id, a.id, 'WH-001', '中心物料仓', '场部西侧',
       '王仓管', '13800000003', 1, 'verified', 1, 1, CURRENT_TIMESTAMP
FROM areas AS a
WHERE a.code = 'A-NORTH'
ON DUPLICATE KEY UPDATE name = VALUES(name);
