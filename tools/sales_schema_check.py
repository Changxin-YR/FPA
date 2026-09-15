"""008_sales 迁移的约束验证：**证明约束真的拦得住**，不只是"存在"。

为什么需要这个脚本：`SHOW CREATE TABLE` 能告诉你约束在，但**不能**告诉你它拦不拦得住。
本项目已记过一次同类假阳性（`docs/DECISIONS.md` 附：`check_source_hygiene.py` 传目录时
静默扫描 0 个文件 → "看着像通过其实是空跑"）。所以这里对每条约束都跑一条
**应当被拒绝**的写入，断言它真的被拒。

## 两个刻意的实现选择

1. **按 errno 判定，不按异常类判定**。MySQL 8 违反 CHECK 给 errno 3819 / SQLSTATE HY000，
   而这台机器上的 pymysql 把它归到 `OperationalError` 而不是 `IntegrityError`。
   按异常类判会把"约束拦住了"写成"脚本崩了"。
2. **父子行的插入顺序显式写出来，并且用真实自增 id 而不是猜 1/2/3**。
   第一版脚本猜了 id，于是子表插入报 1452（外键失败）——那是**脚本的排序 bug**，
   却被打印成"约束误拒"。这类假阴性比假阳性更难发现，因为它看起来像是被测对象的错。

用法（独立库，避免与其它域的 e2e 抢数据 —— ROLLOUT_CONTRACT §5）::

    $env:MYSQL_DATABASE='fpa_sales'
    $env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'
    python tools/bootstrap_db.py
    python tools/migrate.py apply
    python tools/sales_schema_check.py
"""

from __future__ import annotations

import os
import sys

import pymysql

FAILURES = 0

#: 约束类错误码。CHECK 在 MySQL 8 上是 3819，外键是 1452 / 1451，
#: 唯一键是 1062，非空是 1048，长度越界是 1406。
CONSTRAINT_ERRNOS = frozenset({3819, 4025, 1451, 1452, 1048, 1062, 1406})

ORDER_INSERT = (
    "INSERT INTO sales_orders (organization_id,farm_id,area_id,code,name,customer_id,"
    "pond_id,batch_id,species,quantity,unit,unit_price,sold_at,due_date,created_by) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)
ORDER_BAD_STATUS = (
    "INSERT INTO sales_orders (organization_id,farm_id,area_id,code,name,customer_id,"
    "pond_id,batch_id,species,quantity,unit,unit_price,sold_at,due_date,status,created_by) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)
ORDER_CANCEL_NO_REASON = (
    "INSERT INTO sales_orders (organization_id,farm_id,area_id,code,name,customer_id,"
    "pond_id,batch_id,species,quantity,unit,unit_price,sold_at,due_date,status,reason,"
    "created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)
DELIVERY_INSERT = (
    "INSERT INTO deliveries (organization_id,farm_id,area_id,code,name,sales_order_id,"
    "harvest_document_id,batch_id,pond_id,quantity,delivered_at,created_by) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)
RECEIVABLE_INSERT = (
    "INSERT INTO receivables (organization_id,farm_id,area_id,code,name,sales_order_id,"
    "delivery_id,customer_id,total_amount,paid_amount,due_date,occurred_on,created_by) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)
RECEIPT_INSERT = (
    "INSERT INTO sales_receipts (organization_id,farm_id,area_id,code,name,receivable_id,"
    "amount,received_at,receipt_method,occurred_on,created_by) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    if ok:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def main() -> int:
    conn = pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "fpa"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "fpa_sales"),
        autocommit=True,
    )
    cur = conn.cursor()

    def execute(sql: str, params: tuple) -> tuple[bool, int | None, int | None]:
        """返回 (成功?, errno, lastrowid)。"""
        try:
            cur.execute(sql, params)
            return True, None, cur.lastrowid
        except (pymysql.err.IntegrityError, pymysql.err.OperationalError) as error:
            errno = error.args[0] if error.args else None
            return False, errno, None

    def expect_reject(label: str, sql: str, params: tuple) -> None:
        ok, errno, _ = execute(sql, params)
        if ok:
            check(label, False, "写入被接受了 —— 约束没有生效")
        elif errno in CONSTRAINT_ERRNOS:
            check(label, True, f"errno={errno}")
        else:
            check(label, False, f"抛的不是约束错误（errno={errno}）")

    def expect_accept(label: str, sql: str, params: tuple) -> int | None:
        ok, errno, rowid = execute(sql, params)
        if ok:
            check(label, True, f"id={rowid}")
            return rowid
        check(label, False, f"被误拒（errno={errno}）")
        return None

    # -- 清场（幂等，可反复跑） ------------------------------------------------
    for table in ("sales_receipts", "receivables", "deliveries", "sales_orders"):
        cur.execute(f"DELETE FROM {table}")

    # -- 种子 ----------------------------------------------------------------
    cur.execute("INSERT IGNORE INTO organizations (id,code,name) VALUES (1,'VC1','验证企业一'),(2,'VC2','验证企业二')")
    cur.execute("INSERT IGNORE INTO farms (id,organization_id,code,name) VALUES (1,1,'VF1','验证基地')")
    cur.execute(
        "INSERT IGNORE INTO users (id,username,display_name,password_hash,status) "
        "VALUES (1,'vc1','甲','x','active'),(2,'vc2','乙','x','active')"
    )

    print("=== sales_orders ===")
    order_a = expect_accept("合法销售单", ORDER_INSERT,
                            (1, 1, 1, 'SO-1', '正常单', 1, 1, 1, '草鱼', 100, 'kg', 10, '2026-01-10', '2026-02-10', 1))
    expect_reject("due_date 早于 sold_at（§2.9）", ORDER_INSERT,
                  (1, 1, 1, 'SO-2', '日期倒挂', 1, 1, 1, '草鱼', 100, 'kg', 10, '2026-02-10', '2026-01-10', 1))
    expect_reject("unit 不在 kg/jin/tail", ORDER_INSERT,
                  (1, 1, 1, 'SO-3', '坏单位', 1, 1, 1, '草鱼', 100, 'ton', 10, '2026-01-10', '2026-02-10', 1))
    expect_reject("quantity = 0（§2.9 要求 > 0）", ORDER_INSERT,
                  (1, 1, 1, 'SO-4', '零数量', 1, 1, 1, '草鱼', 0, 'kg', 10, '2026-01-10', '2026-02-10', 1))
    expect_reject("unit_price = 0（§2.9 要求 > 0）", ORDER_INSERT,
                  (1, 1, 1, 'SO-5', '零单价', 1, 1, 1, '草鱼', 100, 'kg', 0, '2026-01-10', '2026-02-10', 1))
    # §4 #22「取消必须填原因」的**数据库层底线**。
    # 服务层与声明层会先挡住它，所以献得验的是**绕过服务层的**
    # 写入（导入脚本、运维修数）——那正是迁移里那条 CHECK 存在的理由。
    expect_reject(
        "cancelled 但没有取消原因（§4 #22）",
        ORDER_CANCEL_NO_REASON,
        (1, 1, 1, "SO-CL-1", "取消无原因", 1, 1, 1, "草鱼", 100, "kg", 10,
         "2026-01-10", "2026-02-10", "cancelled", None, 1),
    )
    expect_reject(
        "cancelled 但原因只有空白字符（§4 #22）",
        ORDER_CANCEL_NO_REASON,
        (1, 1, 1, "SO-CL-2", "取消空白原因", 1, 1, 1, "草鱼", 100, "kg", 10,
         "2026-01-10", "2026-02-10", "cancelled", "   ", 1),
    )
    expect_reject("status 非法值", ORDER_BAD_STATUS,
                  (1, 1, 1, 'SO-6', '坏状态', 1, 1, 1, '草鱼', 100, 'kg', 10, '2026-01-10', '2026-02-10', 'bogus', 1))
    expect_reject("code 同企业内重复（§4 #19）", ORDER_INSERT,
                  (1, 1, 1, 'SO-1', '重复编号', 1, 1, 1, '草鱼', 100, 'kg', 10, '2026-01-10', '2026-02-10', 1))
    order_b = expect_accept("code 跨企业可重复（唯一键含 organization_id）", ORDER_INSERT,
                            (2, 1, 1, 'SO-1', '他企业同号', 1, 1, 1, '草鱼', 100, 'kg', 10, '2026-01-10', '2026-02-10', 1))

    print()
    print("=== deliveries（域内 FK + 跨域不建 FK） ===")
    delivery_1 = expect_accept("合法交付", DELIVERY_INSERT,
                               (1, 1, 1, 'DL-1', '交付一', order_a, 999, 1, 1, 40, '2026-01-15 10:00:00', 1))
    expect_reject("sales_order_id 不存在（域内外键生效）", DELIVERY_INSERT,
                  (1, 1, 1, 'DL-2', '孤儿单', 999999, 999, 1, 1, 10, '2026-01-15 10:00:00', 1))
    delivery_3 = expect_accept(
        "harvest_document_id 指向不存在的 production 行也**接受**（跨域不建外键，ROLLOUT_CONTRACT §1.1）",
        DELIVERY_INSERT,
        (1, 1, 1, 'DL-3', '跨域引用', order_a, 888888, 1, 1, 10, '2026-01-15 10:00:00', 1))
    expect_reject("quantity = 0", DELIVERY_INSERT,
                  (1, 1, 1, 'DL-4', '零交付', order_a, 999, 1, 1, 0, '2026-01-15 10:00:00', 1))
    expect_reject("code 同企业内重复", DELIVERY_INSERT,
                  (1, 1, 1, 'DL-1', '重复编号', order_a, 999, 1, 1, 10, '2026-01-15 10:00:00', 1))

    print()
    print("=== receivables ===")
    expect_accept("合法应收", RECEIVABLE_INSERT,
                  (1, 1, 1, 'AR-1', '应收一', order_a, delivery_1, 1, 400, 0, '2026-02-10', '2026-01-15', 1))
    expect_reject("同一交付单生成第二条应收（uq_receivables_delivery）", RECEIVABLE_INSERT,
                  (1, 1, 1, 'AR-2', '重复应收', order_a, delivery_1, 1, 400, 0, '2026-02-10', '2026-01-15', 1))
    expect_reject("paid_amount > total_amount（累计越界）", RECEIVABLE_INSERT,
                  (1, 1, 1, 'AR-3', '超额收款', order_a, delivery_3, 1, 400, 500, '2026-02-10', '2026-01-15', 1))
    expect_reject("paid_amount < 0", RECEIVABLE_INSERT,
                  (1, 1, 1, 'AR-4', '负收款', order_a, delivery_3, 1, 400, -5, '2026-02-10', '2026-01-15', 1))
    receivables_5 = expect_accept("部分收款", RECEIVABLE_INSERT,
                                  (1, 1, 1, 'AR-5', '部分收款', order_a, delivery_3, 1, 400, 150, '2026-02-10', '2026-01-15', 1))

    print()
    print("=== sales_receipts ===")
    expect_accept("合法收款", RECEIPT_INSERT,
                  (1, 1, 1, 'SR-1', '收款一', receivables_5, 150, '2026-01-20 09:00:00', 'bank_transfer', '2026-01-20', 1))
    expect_reject("receipt_method 非法值", RECEIPT_INSERT,
                  (1, 1, 1, 'SR-2', '坏方式', receivables_5, 50, '2026-01-20 09:00:00', 'bitcoin', '2026-01-20', 1))
    expect_reject("amount = 0", RECEIPT_INSERT,
                  (1, 1, 1, 'SR-3', '零金额', receivables_5, 0, '2026-01-20 09:00:00', 'cash', '2026-01-20', 1))
    expect_reject("receivable_id 不存在（域内外键）", RECEIPT_INSERT,
                  (1, 1, 1, 'SR-4', '孤儿收款', 999999, 50, '2026-01-20 09:00:00', 'cash', '2026-01-20', 1))

    print()
    print("=== 落库行数与收尾 ===")
    for table, expected in (("sales_orders", 2), ("deliveries", 2), ("receivables", 2), ("sales_receipts", 1)):
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        actual = int(cur.fetchone()[0])
        check(f"{table} 行数 = {expected}", actual == expected, f"实际 {actual}")

    conn.close()

    print()
    if FAILURES:
        print(f"FAILED: {FAILURES} 项")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
