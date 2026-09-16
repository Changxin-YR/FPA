"""临时诊断（t2 内核不变量）：同一归属对象上第二笔库存归集为什么被拒。

`tools/cost_e2e.py` 把它记成"[已知内核缺陷] NoOverlappingSource 的自排除没生效"。
自排除在源码里是把 `key_column <> %s` **下推**进 SQL 的（`_invariant_exclude_id`
由 `runner._invariant_extra()` 注入），所以先要实证：到底是"没注入"、
"注入了但没进 SQL"，还是"注入了、也进了 SQL，但命中的那一行**真的不是本次自己**"。

用法（临时库 `yuxin_nooverlap_diag`，**绝不碰 `yuxin`**）：

    python tools\\repro_no_overlap_self_exclusion.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from yuxin.kernel.invariants import NoOverlappingSource  # noqa: E402
from yuxin.kernel.scope import Scope  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork  # noqa: E402

PROBE_DB = "yuxin_nooverlap_diag"


def root_connection(database: str | None = None) -> pymysql.Connection:
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_ROOT_USER", "root"),
        password=os.environ.get("MYSQL_ROOT_PASSWORD", "1234"),
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )


def build_fixture() -> None:
    with root_connection() as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {PROBE_DB}")
        cur.execute(f"CREATE DATABASE {PROBE_DB} DEFAULT CHARSET utf8mb4")
    with root_connection(PROBE_DB) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE cost_entries ("
            " id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,"
            " organization_id BIGINT UNSIGNED NOT NULL,"
            " farm_id BIGINT UNSIGNED NOT NULL,"
            " area_id BIGINT UNSIGNED NOT NULL,"
            " source_type VARCHAR(32) NOT NULL,"
            " source_ref VARCHAR(64) NOT NULL,"
            " target_type VARCHAR(16) NOT NULL,"
            " target_id BIGINT UNSIGNED NOT NULL,"
            " period_start DATE NOT NULL,"
            " period_end DATE NOT NULL)"
        )


def where_of(sql: str) -> str:
    return sql.split("WHERE", 1)[-1].strip()


def main() -> int:
    build_fixture()
    config = ConnectionConfig(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_ROOT_USER", "root"),
        password=os.environ.get("MYSQL_ROOT_PASSWORD", "1234"),
        database=PROBE_DB,
    )
    invariant = NoOverlappingSource(
        source_types=("warehouse_ledger",),
        target_fields=("target_type", "target_id"),
        period_fields=("period_start", "period_end"),
        tenant_keys=("organization_id", "farm_id", "area_id"),
    )
    scope = Scope.all_data(user_id=1)

    def create(source_ref: str) -> int:
        with UnitOfWork(config).begin() as tx:
            cur = tx.execute(
                "INSERT INTO cost_entries (organization_id, farm_id, area_id, source_type,"
                " source_ref, target_type, target_id, period_start, period_end)"
                " VALUES (1,1,1,'warehouse_ledger',%s,'pond',7,'2026-09-01','2026-09-30')",
                (source_ref,),
            )
            del cur
            return int(tx.last_insert_id())

    first_id = create("DIAG-FEEDING-992")
    second_id = create("DIAG-FEEDING-993")
    print(f"两行已建：first={first_id} second={second_id}")

    payload = {
        "source_type": "warehouse_ledger",
        "target_type": "pond",
        "target_id": 7,
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "organization_id": 1,
        "farm_id": 1,
        "area_id": 1,
    }

    class Recorder:
        def __init__(self, tx):
            self.tx = tx

        def query_one(self, sql, params=None):
            print("  SQL   :", where_of(sql))
            print("  params:", list(params or ()))
            rows = self.tx.query_all(sql, list(params or ()))
            print("  命中   :", rows)
            return rows[0] if rows else None

    with UnitOfWork(config).begin() as tx:
        print("\n[1] 以**第二笔自己**的 id 做自排除（这是真实的执行器行为）")
        payload["_invariant_exclude_id"] = second_id
        try:
            invariant.check(
                tx=Recorder(tx), scope=scope, actor_id=1, payload=payload, before=None
            )
            print("  -> 放行（正确：除了我以外没有别人）")
        except Exception as error:  # noqa: BLE001
            print(f"  -> 拒绝：{error}")

        print("\n[2] 不做自排除（对照组：应当命中第一笔并拒绝）")
        payload.pop("_invariant_exclude_id")
        try:
            invariant.check(
                tx=Recorder(tx), scope=scope, actor_id=1, payload=payload, before=None
            )
            print("  -> 放行（**不该放行**：库里明明有第一笔）")
        except Exception as error:  # noqa: BLE001
            print(f"  -> 拒绝：{error}")

    with root_connection() as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {PROBE_DB}")
    print(f"\n临时库 {PROBE_DB} 已删除；未触碰 yuxin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
