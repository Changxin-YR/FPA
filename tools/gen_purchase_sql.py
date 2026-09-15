"""生成 database/migrations/007_purchase.sql。

为什么用脚本而不是直接写文件：`docs/DEVELOPMENT.md` §5.1 —— PowerShell 的
`Set-Content` / here-string 会写 BOM 或静默吞内容（本会话中已实测：一次
`python - <<'EOF'` 的 here-doc 无任何输出地失败）。这里用
`Path.write_text(..., encoding='utf-8', newline='\n')` 保证无 BOM + LF。
"""

from __future__ import annotations

import pathlib
import sys

SQL = r'''-- ============================================================================
-- 007 采购：采购单 / 应付 / 付款
--
-- 字段与约束**严格取自** docs/CAPABILITY_REGISTRY.md：
--   §1.7  purchase 域的 10 条能力
--   §2.8  purchase 字段表（key/type/required/约束/label）
--   §3.4  purchase_order 状态机（7 态 + 转移表）
--   §4    #3 #4 #5 #9 #12 #13 #14 #19
--   §0.7  规则 1（分租键由服务端解析写入，不接受客户端提交）、规则 3
--
-- 跨域契约：docs/ROLLOUT_CONTRACT.md
--   §1.1 禁止跨域外键 —— 本迁移只对 organizations / farms / users / areas
--        （基础表）与本域自己的表建外键；
--   §1.2 状态列一律 VARCHAR(32)，**不用 ENUM**；
--   §2   跨域效果只能经具名进程内函数：
--          purchase.purchase_orders.apply_receipt(...)
--          purchase.payables.create_from_receipt(...)
--        由 warehouse 域的 receipt.verify 调用，同一个 tx。
--
-- **表名**：`purchase_payables` / `purchase_payments` 由 评审结论
-- （registry 正文只出现裸名 `payables` / `payments`，而本仓表名一律带域前缀：
-- `purchase_orders` / `production_batches` / `warehouse_documents`）。
-- 两处不一致必须单点定死：迁移建同名表 + `Resource(table=...)` 显式声明 +
-- 不变量 `table=` 写同一字面值。依赖兜底（`<module>_<name>s`）会让
-- "谁省略一次 table=" 得到一个离声明处很远的错误。
--
-- **迁移必须可重入**：MySQL 的 DDL 不回滚，失败后要能直接重跑。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 采购单
--
-- **单物料、单数量**（registry §2.8 的字段表就是这个形态：一个 `material_id`
-- + 一个 `quantity` + 一个 `unit_price`），早期版本库亦然。与 008 的 `sales_orders`
-- 保持同构。
--
-- 为什么不做 `purchase_order_lines` 明细表：`quantity` 一旦同时存在于单头与明细，
-- 就有两处描述同一件事 —— 而 §4 #9 的 `CumulativeWithin` 明确挂在
-- `purchase_orders.quantity` 上（`target_field="quantity"`），内核不变量按
-- **表名/列名**读取，明细表会迫使它与 `apply_receipt` 各自 JOIN 一次。
-- 销售侧同理把数量放在单头（`delivery` 的累计对 `sales_orders.quantity`）。
--
-- `received_quantity` / `total_amount` / `unpaid_amount` 等派生列**不进表**：
-- 它们可由到货明细 / `purchase_payables` / 本表算出。早期版本把汇总冗余存下，
-- 再靠三处回查保持同步；那正是本项目要删掉的"两处描述同一件事"。
--
-- **状态机（§3.4，7 态）**：`closed` 保留在枚举里但**无触发能力**（registry
-- 显式标注"预留、不可达"）；`disputed` **删除**（全仓无写入点）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS purchase_orders (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- 分租键：create 时由服务端从 DataScope 解析写入，
  -- **不接受客户端提交**（registry §0.7 规则 1 / DECISIONS.md Q7）。
  -- 有它们，`resource(area_id)` 的谓词才能直接命中本表列并走索引（Q6 裁决）。
  organization_id       BIGINT UNSIGNED NOT NULL,
  farm_id               BIGINT UNSIGNED NOT NULL,
  area_id               BIGINT UNSIGNED NOT NULL,

  code                  VARCHAR(64)     NOT NULL,
  name                  VARCHAR(100)    NOT NULL,
  -- 供应商 / 物料 / 收货仓：**只存 BIGINT，不建跨域外键**（§1.1）。
  -- 外键会把五个域的迁移变成全序依赖；约束由不变量负责（`SameTenant` /
  -- `ReferencedStatus`）。supplier_id 指向 `business_partners`（003），
  -- warehouse_id 指向 warehouse 域的表（006），两者都不是本域的表。
  supplier_id           BIGINT UNSIGNED NOT NULL,
  material_id           BIGINT UNSIGNED NOT NULL,
  warehouse_id          BIGINT UNSIGNED NOT NULL,
  quantity              DECIMAL(16,3)   NOT NULL,
  unit_price            DECIMAL(14,4)   NOT NULL,
  expected_delivery_date DATE           NOT NULL,
  due_date              DATE            NOT NULL,
  -- 取消时必须填原因（§4「取消必须填原因」）。旧 `purchase_service.py:151-153`。
  -- 它同时是"取消"这个动作的审计依据。
  reason         VARCHAR(500)    NULL,
  note                  VARCHAR(500)    NULL,

  status                VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version           INT UNSIGNED    NOT NULL DEFAULT 1,

  created_by            BIGINT UNSIGNED NOT NULL,
  updated_by            BIGINT UNSIGNED NULL,
  -- `approved_by`（不是 verified_by）：采购单是**三段式**（提交→审批→执行），
  -- 与单据类（两段式 draft→verified）刻意不同（registry §3.6 的分类依据）。
  -- `DistinctActors(creator_field="created_by", verifier_field="approved_by")` 按此声明。
  approved_by           BIGINT UNSIGNED NULL,
  approved_at           DATETIME        NULL,
  created_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  -- §4 #19 编码在范围内唯一。DB 唯一键是**并发下唯一可信**的判定；
  -- 应用层 `UniqueCode` 只负责给出可读的 409（早期版本靠异常消息文本判断：
  -- `早期版本 production_store.py:142-146` 的 `if key not in str(exc)`）。
  UNIQUE KEY uq_purchase_orders_org_code (organization_id, code),
  KEY idx_purchase_orders_scope_status (organization_id, farm_id, area_id, status),
  KEY idx_purchase_orders_supplier (supplier_id),
  KEY idx_purchase_orders_material_warehouse (material_id, warehouse_id),
  -- 逾期应付的扫描路径（due_date + status）
  KEY idx_purchase_orders_due (organization_id, due_date, status),
  -- warehouse 的 `receipt.create` / `receipt.verify` 按 `purchase_order_id` 反查本表，
  -- 而 `CumulativeWithin(target_dims=("purchase_order_id",), target_columns=("id",))`
  -- 也按主键定位。这条索引是那两条不变量的查询路径。
  KEY idx_purchase_orders_status_id (status, id),

  CONSTRAINT fk_purchase_orders_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_orders_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_orders_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_orders_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_orders_updated_by FOREIGN KEY (updated_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_orders_approved_by FOREIGN KEY (approved_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  -- 状态枚举放数据库层：应用层写错一格只会静默产生一个前端渲染不出来的行。
  -- `closed` 在枚举里但**无触发能力**（§3.4 显式标注"预留、不可达"）；
  -- `disputed` 已删除（保留它会让"状态是否可达"这条检查失去意义）。
  CONSTRAINT chk_purchase_orders_status CHECK (
    status IN ('draft','submitted','approved','partially_received',
               'fully_received','cancelled','closed')
  ),
  -- registry §2.8：quantity > 0、unit_price > 0
  CONSTRAINT chk_purchase_orders_quantity CHECK (quantity > 0),
  CONSTRAINT chk_purchase_orders_unit_price CHECK (unit_price > 0),
  -- registry §2.8："due_date 不得早于 expected_delivery_date"
  -- （旧 `purchase_service.py:52-60` 的 `_validate_dates`）。应用层也要校验
  -- （要给字段级错误），但数据库这层保证它**不可能**被绕过 —— 早期版本只有
  -- 应用层校验，于是导入/修数脚本可以写出日期倒挂的单据。
  CONSTRAINT chk_purchase_orders_dates CHECK (due_date >= expected_delivery_date),
  -- 取消必须留原因。理由不是"表单要求"：取消是**不可逆的对外承诺撤销**，
  -- 没有原因的 cancelled 行无法审计。
  CONSTRAINT chk_purchase_orders_reason CHECK (
    status <> 'cancelled' OR (reason IS NOT NULL AND reason <> '')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 应付账款
--
-- **由 `receipt.verify` 生成，没有独立的新建能力**（§1.7 只有 `payable.list`）。
-- 与 008 的 `receivables` 同构 —— 两侧是同一件事的两个方向，字段集刻意一致
-- （`total_amount` / `paid_amount` / `currency` / `due_date` / `occurred_on`），
-- 这样 §4 #4 的 `AmountWithin` 在两侧能用同一套参数。
--
-- `paid_amount` 是**累计已付**，只由 `payment.verify` 推进；
-- `balance` 不存列 —— 它是 `total_amount - paid_amount`，存下来就是冗余。
-- §4 #4 的余额算法由内核显式声明（`余额 = balance_column - paid_column`），
-- 这里 `balance_column="total_amount"`。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS purchase_payables (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  organization_id       BIGINT UNSIGNED NOT NULL,
  farm_id               BIGINT UNSIGNED NOT NULL,
  area_id               BIGINT UNSIGNED NOT NULL,

  -- 应付**没有 code**：它是派生单据，不是人填的单据。人工编号在这里只会变成
  -- 一个没人维护的冗余字段（早期版本给它编号，然后没有任何查询用它）。
  name                  VARCHAR(100)    NOT NULL,
  purchase_order_id     BIGINT UNSIGNED NOT NULL,
  -- 指向 warehouse 域的到货单：**只存 BIGINT，不建跨域外键**（§1.1）。
  -- 作用有二：① 让"一张到货单只生成一张应付"可判定（下面的唯一键）；
  -- ② 让成本归集能追溯到那张到货单。
  receipt_id            BIGINT UNSIGNED NOT NULL,
  supplier_id           BIGINT UNSIGNED NOT NULL,
  total_amount          DECIMAL(16,2)   NOT NULL,
  paid_amount           DECIMAL(16,2)   NOT NULL DEFAULT 0,
  -- §4 #4 的 `AmountWithin` 会读它做币种一致性判断。跨币种登记必须被拒，
  -- 而不是按汇率静默换算。
  currency              CHAR(3)         NOT NULL DEFAULT 'CNY',
  due_date              DATE            NOT NULL,
  -- 期间锁定的比较对象（`PeriodOpen(date_field="occurred_on")`）：
  -- 应付的入账日就是它对应的到货发生日。
  occurred_on           DATE            NOT NULL,

  status                VARCHAR(32)     NOT NULL DEFAULT 'unpaid',
  row_version           INT UNSIGNED    NOT NULL DEFAULT 1,

  created_by            BIGINT UNSIGNED NOT NULL,
  created_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  -- **一张到货单只能生成一条应付**："重复核验不得重复生成应付"的物理保证。
  -- 核验路径是"查有没有 -> 没有则插"，并发下两次核验会双插，唯一键是唯一可信的
  -- 判定（应用层"先查再插"做不到）。`create_from_receipt` 靠它实现**结构上幂等**
  -- （重放返回 created=False 而不报错）。
  UNIQUE KEY uq_purchase_payables_receipt (receipt_id),
  KEY idx_purchase_payables_scope_status (organization_id, farm_id, area_id, status),
  KEY idx_purchase_payables_order (purchase_order_id),
  KEY idx_purchase_payables_due (organization_id, due_date, status),
  -- §4 #4 `AmountWithin` 的查询路径
  KEY idx_purchase_payables_paid (id, status, paid_amount),

  CONSTRAINT fk_purchase_payables_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payables_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payables_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- 域内 FK（同一份迁移）**保留**：去掉它会让"应付挂空"成为可能。
  -- §1.1 禁的是**跨域**外键。
  CONSTRAINT fk_purchase_payables_order FOREIGN KEY (purchase_order_id)
    REFERENCES purchase_orders(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payables_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  -- 只写**可达**状态（§1.7 的 `AmountWithin` 用 ("unpaid","partial") 作为可付款集合）。
  -- 早期版本库枚举还有 settled / overpaid / bad_debt：
  --   overpaid —— `AmountWithin` 挡在源头（付款金额 ≤ 余额），产生不了超付；
  --   bad_debt —— 核销需要独立能力，本版没有（registry §6 明确不做）。
  -- 枚举里放一个永远达不到的值，会让"状态是否可达"这条检查失去意义。
  CONSTRAINT chk_purchase_payables_status CHECK (
    status IN ('unpaid','partial','settled')
  ),
  CONSTRAINT chk_purchase_payables_total CHECK (total_amount > 0),
  -- 累计已付不得超过应付总额且不得为负。§4 #4 的 `AmountWithin` 在**写入前**挡住
  -- "本次金额超余额"，这条 CHECK 在**写入后**保证累计值不可能越界 ——
  -- 两层各管一件事：不变量给可读文案，CHECK 保证数据本身不带病。
  CONSTRAINT chk_purchase_payables_paid CHECK (
    paid_amount >= 0 AND paid_amount <= total_amount
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 付款单
--
-- 两态（registry §3.6 的"单据类统一两态"）：`draft -> verified`。
-- 付款是两段式（经办人录入 -> 核验人核验），没有审批层 —— 与 `sales_receipts` 同构。
--
-- `payment.verify` 是**两条不变量的强制点**：
--   §4 #4  `AmountWithin` —— 付款不得超过应付余额
--   §4 #5  `DistinctActors(created_by, verified_by)` —— 经办人不得自己核验
-- 早期版本把 #4 在 create（`早期版本 purchase_payment_store.py:114-115`）与 verify
-- （`:154-157`）**两处**重复实现，且**没有** #5。本版只声明一处（verify），
-- 不把旧缺陷抄回来；#5 不留自审例外（Q8 裁决）。
--
-- 字段名 `payment_method` 与销售侧的 `receipt_method` **故意不统一**（§2.9）：
-- 旧前端两个页面各自命名（`PayablePage.vue` / `ReceivablePage.vue` 的渲染分支），
-- 改名会破坏继承。这是"字段名继承原则"（§2.1）的硬约束，不是疏忽 ——
-- 所以必须写一句说明，否则下一个人会"顺手统一"它。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS purchase_payments (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  organization_id       BIGINT UNSIGNED NOT NULL,
  farm_id               BIGINT UNSIGNED NOT NULL,
  area_id               BIGINT UNSIGNED NOT NULL,

  code                  VARCHAR(64)     NOT NULL,
  name                  VARCHAR(100)    NOT NULL,
  payable_id            BIGINT UNSIGNED NOT NULL,
  amount                DECIMAL(16,2)   NOT NULL,
  -- registry §2.8：`paid_at` datetime，**不得晚于当前时间**。期间锁定所需的
  -- `occurred_on` 由服务从它取日期（`paid_at.date()`），**不额外存一列** ——
  -- 否则同一事实有两个载体。
  paid_at               DATETIME        NOT NULL,
  -- 枚举五值继承旧 `purchase_service.py:17`：bank_transfer / cash / check /
  -- digital_wallet / other。
  payment_method        VARCHAR(32)     NOT NULL,
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
  UNIQUE KEY uq_purchase_payments_org_code (organization_id, code),
  KEY idx_purchase_payments_scope_status (organization_id, farm_id, area_id, status),
  -- §4 #4 `AmountWithin` 的查询路径
  KEY idx_purchase_payments_payable_status (payable_id, status),

  CONSTRAINT fk_purchase_payments_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payments_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payments_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- 域内 FK：同 007 的理由，保留。
  CONSTRAINT fk_purchase_payments_payable FOREIGN KEY (payable_id)
    REFERENCES purchase_payables(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payments_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payments_updated_by FOREIGN KEY (updated_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_purchase_payments_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  CONSTRAINT chk_purchase_payments_status CHECK (status IN ('draft','verified')),
  CONSTRAINT chk_purchase_payments_amount CHECK (amount > 0),
  CONSTRAINT chk_purchase_payments_method CHECK (
    payment_method IN ('bank_transfer','cash','check','digital_wallet','other')
  ),
  -- §4 #5：经办人不得核验自己录的付款。落在**数据库层**，连脏数据都写不进去。
  -- 应用层的不变量（`DistinctActors`）与服务的显式判断同样要有（第三层防御），
  -- 但"哪一层都不该放行"这件事由约束保证最可靠。
  CONSTRAINT chk_purchase_payments_distinct_actors CHECK (
    verified_by IS NULL OR verified_by <> created_by
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- 种子
--
-- **本迁移自包含**：不假设前置数据存在。
--
-- 刻意**不种采购单/应付/付款探针数据**：它们是业务单据，种出来只会让
-- "列表页有多少条"这个问题的答案依赖迁移历史。区域 / 物料 / 供应商已由 003 种好，
-- e2e 脚本自己建探针行并清理（见 `tools/purchase_e2e.py`）。
--
-- 也刻意**不种权限码**（与 008_sales.sql 同一理由，已上报 负责人）：
-- `permissions` 表的权威来源是能力声明的 `required_permission`（REGISTRY），
-- 在迁移里再 INSERT 一遍就是"两处描述同一件事"。种子应**从能力声明派生**，
-- 当前仓库还没有这个入口（`SELECT COUNT(*) FROM permissions;` -> 0），
-- 这是影响全部 69 条能力的基础设施缺口，不属于本域。
-- ============================================================================
'''

TARGET = pathlib.Path(__file__).resolve().parents[1] / "database" / "migrations" / "007_purchase.sql"


def main(argv: list[str] | None = None) -> int:
    """`--check` 只比对不写入；不带参数则写入。四种结果都带退出码。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check" in args:
        if not TARGET.exists():
            print(f"MISSING: {TARGET}")
            return 1
        if TARGET.read_text(encoding="utf-8") != SQL:
            print("DRIFT: 007_purchase.sql 与生成脚本不一致")
            return 1
        print(f"OK: {TARGET} 与生成脚本一致（{len(SQL)} 字符）")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(SQL, encoding="utf-8", newline="\n")
    raw = TARGET.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "写了 BOM"
    assert b"\r\n" not in raw, "写了 CRLF"
    print(f"WROTE: {TARGET} {len(raw)} bytes, BOM=no, CRLF=no, lines={SQL.count(chr(10))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
