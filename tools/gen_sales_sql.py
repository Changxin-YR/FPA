"""生成 database/migrations/008_sales.sql。

为什么用脚本生成而不是直接编辑 .sql：
`docs/DEVELOPMENT.md` §5.1 与 `docs/ROLLOUT_CONTRACT.md` §5 都记了同一条坑——
PowerShell 写文件会写 BOM（MySQL 报 `syntax error near '\ufeff'`）或静默失败。
`Path.write_text(..., encoding='utf-8', newline='\n')` 是唯一可靠的做法。

用法：
    python tools/gen_sales_migration.py            # 写入 database/migrations/008_sales.sql
    python tools/gen_sales_migration.py --check    # 只校验，不写入
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "database" / "migrations" / "008_sales.sql"

SQL = """-- ============================================================================
-- 008 销售：销售单 / 交付单 / 应收 / 收款单
--
-- 字段与约束**严格取自** docs/CAPABILITY_REGISTRY.md：
--   §1.8  sales 域的 12 条能力
--   §2.9  sales 字段表（sales_order.create / delivery.create / sales_receipt.create）
--   §2.11 通用 action 载荷（expected_version / reason）
--   §3.5  sales_order 状态机（与 purchase_order 完全同构）
--   §3.6  单据类统一两态（delivery / sales_receipt 均为 draft -> verified）
--   §4 #5  DistinctActors(created_by, verified_by)
--   §4 #6  HarvestQuantityMatch（交付数量与出塘事实一致）
--   §4 #7  PeriodOpen（delivery.verify 写收入侧，关账一视同仁 —— Q16）
--   §4 #10 CumulativeWithin（累计交付不得超过销售数量）
--   §4 #12 StatusAllowsEdit（核验后只读）
--   §4 #13 OptimisticLock
--   §4 #14 StateTransition
--   §4 #19 UniqueCode（code 在 organization 范围内唯一）
--
-- 三处**刻意的设计选择**，理由都写在下面各自的段落里：
--   1. 状态列一律 `VARCHAR(32)`，不用 ENUM（ROLLOUT_CONTRACT §1.2 / registry §3.2）
--   2. **不建任何跨域外键**（ROLLOUT_CONTRACT §1.1）：
--      `deliveries.harvest_document_id` 指 production 的 `harvests`，只存 BIGINT，
--      约束由 `HarvestQuantityMatch` 不变量负责，不由数据库负责
--   3. 应收的累计已收款用**唯一入口**维护（sales.receivables.create_from_delivery
--      与 sales_receipt.verify），而不是靠触发器 —— 触发器是"第三个地方写同一件事"
--
-- **迁移必须可重入**：MySQL 的 DDL 不回滚，失败后要能直接重跑。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 销售单
--
-- 状态机（registry §3.5，7 态，与 purchase_order 逐字同构）：
--   draft -> submitted -> approved -> partially_delivered -> fully_delivered
--                                  -> cancelled
--   closed 保留在枚举里但**无触发能力**（显式标注"不可达"，继承早期版本形态）
--
-- 为什么不存 `disputed`：registry §3.5 明确删除 —— 全仓无任何代码写入该状态
-- （旧 `013_sales_receivables.sql:22` 有它，但没有任何写入点）。
-- 枚举里放一个永远达不到的值，会让"状态是否可达"这条检查失去意义。
--
-- 派生列（§2.9）：customer_name / pond_name / batch_code / delivered_quantity /
-- total_amount / balance **不进表** —— 它们全部可由本表 + deliveries + receivables
-- 算出。早期版本把它们冗余存下，然后靠三处回查保持同步；那正是本项目要删掉的
-- "两处描述同一件事"（`docs/DEVELOPMENT.md` §4）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sales_orders (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- 分租键：create 时由 ScopeResolver 从 pond_id 解析写入，
  -- **不接受客户端提交**（registry §0.7 规则 1 / DECISIONS.md Q7）
  organization_id       BIGINT UNSIGNED NOT NULL,
  farm_id               BIGINT UNSIGNED NOT NULL,
  area_id               BIGINT UNSIGNED NOT NULL,

  code                  VARCHAR(64)     NOT NULL,
  name                  VARCHAR(100)    NOT NULL,
  customer_id           BIGINT UNSIGNED NOT NULL,
  pond_id               BIGINT UNSIGNED NOT NULL,
  batch_id              BIGINT UNSIGNED NOT NULL,
  species               VARCHAR(64)     NOT NULL,
  quantity              DECIMAL(16,3)   NOT NULL,
  unit                  VARCHAR(32)     NOT NULL DEFAULT 'kg',
  unit_price            DECIMAL(14,4)   NOT NULL,
  sold_at               DATE            NOT NULL,
  due_date              DATE            NOT NULL,
  note                  VARCHAR(500)    NULL,
  -- 取消原因。**列名与字段名同名 reason**（§2.11：本版把早期版本的
  -- cancellation_reason 统一为 reason）。不引入字段名到列名的映射，
  -- 那是本项目要删掉的那类东西。
  -- 与 note 分列而不是复用 note：note 是可选的备注，reason 是取消时的**强制**留痕
  -- （§4 #22 取消必须填原因 + §2.11 的必填能力表），两者语义不同。
  reason                VARCHAR(500)    NULL,

  status                VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version           INT UNSIGNED    NOT NULL DEFAULT 1,

  created_by            BIGINT UNSIGNED NOT NULL,
  updated_by            BIGINT UNSIGNED NULL,
  -- `approved_by`（不是 `verified_by`）：销售单是**三段式**（提交 → 审批 → 执行），
  -- 与单据类（两段式 `draft -> verified`，见 `deliveries` / `sales_receipts`）
  -- 刻意不同 —— 这正是 registry §3.6 用来分类的依据。
  -- 口径与采购单**逐字一致**（`007_purchase.sql` 的同一段注释）。
  -- `DistinctActors(verifier_field="approved_by")` 按此声明。
  approved_by           BIGINT UNSIGNED NULL,
  approved_at           DATETIME        NULL,
  created_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  -- §4 #19 编码在范围内唯一。DB 唯一键是**并发下唯一可信的**判定；
  -- 应用层的 `UniqueCode` 只负责把它翻译成可读的 409（早期版本的做法是靠异常
  -- 消息文本判断：旧 `production_store.py:142-146` 的 `if key not in str(exc)`）。
  UNIQUE KEY uq_sales_orders_org_code (organization_id, code),
  KEY idx_sales_orders_scope_status (organization_id, farm_id, area_id, status),
  KEY idx_sales_orders_customer (customer_id),
  KEY idx_sales_orders_pond_batch (pond_id, batch_id),
  -- 逾期应收的扫描路径（due_date + status），早期版本缺这个索引
  KEY idx_sales_orders_due (organization_id, due_date, status),

  CONSTRAINT fk_sales_orders_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_sales_orders_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_sales_orders_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_sales_orders_updated_by FOREIGN KEY (updated_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- 审批列的 FK 叫 approved_by，与上面注释同源（订单类是三段式）
  CONSTRAINT fk_sales_orders_approved_by FOREIGN KEY (approved_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  -- 状态枚举放数据库层：应用层写错一格只会静默产生一个前端渲染不出来的行
  CONSTRAINT chk_sales_orders_status CHECK (
    status IN ('draft','submitted','approved','partially_delivered',
               'fully_delivered','cancelled','closed')
  ),
  CONSTRAINT chk_sales_orders_unit CHECK (unit IN ('kg','jin','tail')),
  -- registry §2.9：quantity > 0、unit_price > 0（两条都是 ✔ 约束）
  CONSTRAINT chk_sales_orders_quantity CHECK (quantity > 0),
  CONSTRAINT chk_sales_orders_unit_price CHECK (unit_price > 0),
  -- registry §2.9："due_date 不得早于 sold_at"。
  -- 应用层也要校验（要给字段级错误），但数据库这层保证它**不可能**被绕过——
  -- 早期版本只有应用层校验，于是导入/修数脚本可以写出日期倒挂的单据。
  CONSTRAINT chk_sales_orders_dates CHECK (due_date >= sold_at),
  -- §4 #22「取消必须填原因」的**数据库层底线**。
  -- 三层各守一件事（registry §4 #22 的施工要求原文）：
  --   1. 不变量 RequiredWhen 让规则在声明处可见；
  --   2. 服务层给**字段级**文案（用户知道该填哪个框）；
  --   3. 这条 CHECK 保证脏数据写不进去（导入/修数脚本也守规矩）。
  -- 三者不是重复判定，各自守一层。
  CONSTRAINT chk_sales_orders_reason CHECK (
    status <> 'cancelled' OR (reason IS NOT NULL AND CHAR_LENGTH(TRIM(reason)) > 0)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 交付单
--
-- 两态（§3.6）：draft -> verified。核验后只读（§4 #12 StatusAllowsEdit）。
--
-- `harvest_document_id` 是两件事实的载体：
--   §4 #6  HarvestQuantityMatch —— 交付数量必须与出塘事实一致
--                                （tail 用 harvest.quantity，否则 weight_kg×2 for jin）
--   §2.9   同批次同塘口 —— 出塘单必须属于本交付的 batch_id / pond_id
-- **只存 BIGINT、不建外键**（ROLLOUT_CONTRACT §1.1）：建了外键，五个域的迁移
-- 就变成全序依赖，无法并行；且"改业务规则要改表结构"正是早期版本 8 次
-- `ALTER TABLE ... MODIFY status ENUM(...)` 的成因。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS deliveries (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  organization_id       BIGINT UNSIGNED NOT NULL,
  farm_id               BIGINT UNSIGNED NOT NULL,
  area_id               BIGINT UNSIGNED NOT NULL,

  code                  VARCHAR(64)     NOT NULL,
  name                  VARCHAR(100)    NOT NULL,
  sales_order_id        BIGINT UNSIGNED NOT NULL,
  -- 跨域引用（production.harvests）：不加外键，约束由 HarvestQuantityMatch 负责
  harvest_document_id   BIGINT UNSIGNED NOT NULL,
  batch_id              BIGINT UNSIGNED NOT NULL,
  pond_id               BIGINT UNSIGNED NOT NULL,
  quantity              DECIMAL(16,3)   NOT NULL,
  delivered_at          DATETIME        NOT NULL,
  transport_info        VARCHAR(200)    NULL,
  acceptance_note       VARCHAR(500)    NULL,
  note                  VARCHAR(500)    NULL,

  status                VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version           INT UNSIGNED    NOT NULL DEFAULT 1,

  created_by            BIGINT UNSIGNED NOT NULL,
  updated_by            BIGINT UNSIGNED NULL,
  verified_by           BIGINT UNSIGNED NULL,
  verified_at           DATETIME        NULL,
  created_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_deliveries_org_code (organization_id, code),
  KEY idx_deliveries_scope_status (organization_id, farm_id, area_id, status),
  -- §4 #10 CumulativeWithin 的查询路径：按销售单 + status 求和
  KEY idx_deliveries_order_status (sales_order_id, status),
  KEY idx_deliveries_harvest (harvest_document_id),

  CONSTRAINT fk_deliveries_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_deliveries_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_deliveries_order FOREIGN KEY (sales_order_id)
    REFERENCES sales_orders(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_deliveries_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_deliveries_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  CONSTRAINT chk_deliveries_status CHECK (status IN ('draft','verified')),
  CONSTRAINT chk_deliveries_quantity CHECK (quantity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 应收
--
-- **由 `delivery.verify` 生成，没有独立的新建能力**（§2.9 只有 receivable.list，
-- §1.8 里没有 receivable.create）。这是与采购侧 payable 同构的安排。
--
-- 状态集来自早期版本库（`早期版本 012_purchase_hardening.sql:4`、`013:98`）：
--   unpaid / partial / settled / overpaid / bad_debt
-- 本版**只写** unpaid / partial / settled 三个：
--   overpaid —— AmountWithin 挡在源头（收款金额 ≤ 余额），产生不了超收
--   bad_debt —— 核销需要独立能力，本版没有（registry §6 明确不做）
-- 保留在 CHECK 里是为了与旧数据的语义对齐，且显式标注"不可达"比偷偷省略好。
--
-- `paid_amount` 是**累计已收款**，只由 sales_receipt.verify 推进；
-- `balance` 不存列 —— 它是 total_amount - paid_amount，存下来就是冗余。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS receivables (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  organization_id       BIGINT UNSIGNED NOT NULL,
  farm_id               BIGINT UNSIGNED NOT NULL,
  area_id               BIGINT UNSIGNED NOT NULL,

  code                  VARCHAR(64)     NOT NULL,
  name                  VARCHAR(100)    NOT NULL,
  sales_order_id        BIGINT UNSIGNED NOT NULL,
  delivery_id           BIGINT UNSIGNED NOT NULL,
  customer_id           BIGINT UNSIGNED NOT NULL,
  total_amount          DECIMAL(16,2)   NOT NULL,
  paid_amount           DECIMAL(16,2)   NOT NULL DEFAULT 0,
  currency              CHAR(3)         NOT NULL DEFAULT 'CNY',
  due_date              DATE            NOT NULL,
  occurred_on           DATE            NOT NULL,

  status                VARCHAR(32)     NOT NULL DEFAULT 'unpaid',
  row_version           INT UNSIGNED    NOT NULL DEFAULT 1,

  created_by            BIGINT UNSIGNED NOT NULL,
  created_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_receivables_org_code (organization_id, code),
  -- **一张交付单只能生成一条应收**。这是"重复核验不得重复生成应收"的物理保证：
  -- 核验路径是"查有没有 → 没有则插"，并发下两次核验会双插，唯一键是唯一可信的判定。
  UNIQUE KEY uq_receivables_delivery (delivery_id),
  KEY idx_receivables_scope_status (organization_id, farm_id, area_id, status),
  KEY idx_receivables_order (sales_order_id),
  KEY idx_receivables_due (organization_id, due_date, status),

  CONSTRAINT fk_receivables_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_receivables_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_receivables_order FOREIGN KEY (sales_order_id)
    REFERENCES sales_orders(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_receivables_delivery FOREIGN KEY (delivery_id)
    REFERENCES deliveries(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_receivables_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  CONSTRAINT chk_receivables_status CHECK (
    status IN ('unpaid','partial','settled','overpaid','bad_debt')
  ),
  CONSTRAINT chk_receivables_total CHECK (total_amount > 0),
  -- 累计已收不得超过应收总额，且不得为负。
  -- §4 #4 的 AmountWithin 在**写入前**挡住"本次金额超余额"，
  -- 这条 CHECK 在**写入后**保证累计值不可能越界 —— 两层各管一件事：
  -- 不变量给可读文案，CHECK 保证数据本身不带病。
  CONSTRAINT chk_receivables_paid CHECK (paid_amount >= 0 AND paid_amount <= total_amount)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 收款单
--
-- 两态（§3.6）：draft -> verified。
--
-- 字段名 `receipt_method` 与采购侧的 `payment_method` **故意不统一**（§2.9）：
-- 旧前端两个页面各自命名（`PayablePage.vue` / `ReceivablePage.vue` 的表单渲染
-- 分支分别用 payment_method / receipt_method），改名会破坏继承。
-- 这是本项目"字段名继承原则"（§2.1）的**硬约束**，不是疏忽——所以这个不一致
-- 必须在迁移里写一句说明，否则下一个人会"顺手统一"它。
-- 枚举五值继承旧 `purchase_service.py:17`。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sales_receipts (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  organization_id       BIGINT UNSIGNED NOT NULL,
  farm_id               BIGINT UNSIGNED NOT NULL,
  area_id               BIGINT UNSIGNED NOT NULL,

  code                  VARCHAR(64)     NOT NULL,
  name                  VARCHAR(100)    NOT NULL,
  receivable_id         BIGINT UNSIGNED NOT NULL,
  amount                DECIMAL(16,2)   NOT NULL,
  received_at           DATETIME        NOT NULL,
  receipt_method        VARCHAR(32)     NOT NULL,
  occurred_on           DATE            NOT NULL,
  note                  VARCHAR(500)    NULL,

  status                VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version           INT UNSIGNED    NOT NULL DEFAULT 1,

  created_by            BIGINT UNSIGNED NOT NULL,
  updated_by            BIGINT UNSIGNED NULL,
  verified_by           BIGINT UNSIGNED NULL,
  verified_at           DATETIME        NULL,
  created_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_sales_receipts_org_code (organization_id, code),
  KEY idx_sales_receipts_scope_status (organization_id, farm_id, area_id, status),
  -- §4 #4 AmountWithin 的查询路径
  KEY idx_sales_receipts_receivable_status (receivable_id, status),

  CONSTRAINT fk_sales_receipts_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_sales_receipts_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_sales_receipts_receivable FOREIGN KEY (receivable_id)
    REFERENCES receivables(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_sales_receipts_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_sales_receipts_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  CONSTRAINT chk_sales_receipts_status CHECK (status IN ('draft','verified')),
  CONSTRAINT chk_sales_receipts_amount CHECK (amount > 0),
  CONSTRAINT chk_sales_receipts_method CHECK (
    receipt_method IN ('bank_transfer','cash','check','digital_wallet','other')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 本迁移**刻意不种权限码**，理由如下（已上报 负责人）
--
-- §1.8 的 12 条能力一共给出 8 个权限码：sales.view / sales.create /
-- sales.approve / sales.deliver / sales.verify / finance.receivable.view /
-- finance.receipt.manage / finance.receipt.verify（`sales.manage` 已废弃）。
--
-- 但它们**不在这里 INSERT**，因为那会把"销售能力有哪些权限码"变成两处声明：
-- 一处是 `domains/sales/capabilities.py` 的 `required_permission`（权威），
-- 一处是本迁移的 INSERT。两者一旦漂移，症状是"新装系统里每个角色都无权执行
-- 任何销售能力"，而报错文案是"权限不足"——看起来像配置错误，其实是迁移漏了。
-- 这正是 `docs/DEVELOPMENT.md` §4 要根除的形态。
--
-- 正确的位置是**从能力声明派生种子**（`yuxin/kernel/capability.py` 的 REGISTRY
-- 是唯一的权限码来源）。当前仓库**还没有**这个种子入口：`permissions` 表
-- 在 001 里只建了表、没有任何 INSERT，`tools/bootstrap_db.py` 也不种它。
-- 因此这是一个**独立的基础设施缺口**，影响全部 68 条能力，不属于 sales 域。
-- 我按 ROLLOUT_CONTRACT §6 的纪律"不自己拍板"，留给 评审结论。
--
-- 复现命令（现状为空表）：
--   SELECT COUNT(*) FROM permissions;   -- 0
-- ---------------------------------------------------------------------------
"""


def main() -> int:
    check_only = "--check" in sys.argv
    if check_only:
        if not TARGET.exists():
            print(f"MISSING: {TARGET}")
            return 1
        current = TARGET.read_text(encoding="utf-8")
        if current != SQL:
            print("DRIFT: 008_sales.sql 与生成脚本不一致")
            return 1
        print(f"OK: {TARGET} 与生成脚本一致（{len(SQL)} 字符）")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # newline='\n' 是关键：默认会把 \n 变成平台默认换行
    TARGET.write_text(SQL, encoding="utf-8", newline="\n")
    print(f"WROTE: {TARGET} ({len(SQL)} 字符)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
