"""清理 UI E2E 探针留下的行。

## 为什么需要它（P2 实测）

`create-then-list.spec.ts` 与 `list-refresh.spec.ts` 各新建一条**往来单位**，
`master-data-maintain.spec.ts` 新建一个**区域**（然后归档）。三者都没有清理：

  * `partner` / `area` **没有删除能力**（registry 里就没有 delete）；
  * 于是"跑完全绿"之后库里会多出这些行 —— 实测往来单位累积到 7 条，而交付文档写 3 条。
    「测试不是幂等的」这条比功能缺陷更隐蔽：它每次都绿，只是把脏数据留在库里。

## 判据为什么是前缀

两条 spec 的 code 都是自己定的**唯一前缀**（`UI-NEW-<ts>` / `E2E-CACHE-<ts>` /
`E2E-AREA-<ts>`），所以按前缀删是确定且安全的作用域；再加"没有被业务单据引用"
这一条，避免误删真被引用的行。

由 `frontend/playwright.config.ts` 的 `globalTeardown` 在整轮结束时调用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pymysql  # noqa: E402

from fpa.kernel.uow_factory import connection_config  # noqa: E402

#: 往来单位探针前缀（两条 spec 各一个）。
_PARTNER_PREFIXES = ("UI-NEW-%", "E2E-CACHE-%")
#: 区域探针前缀（`master-data-maintain` 新建后归档，不会自己被删）。
_AREA_PREFIXES = ("E2E-AREA-%",)


def main() -> int:
    # Playwright 的 globalTeardown 用 `execFileSync('python', ...)` 调本脚本：子进程
    # 继承的是 **Playwright 进程**的环境。实测直接 `npm run e2e`（没在 shell 里导出
    # `MYSQL_PASSWORD`）时，清理报 1045、整轮退出码变 1。这里给开发库口令一个显式
    # 兜底（与 `tools/bootstrap_db.py` 同一默认值）；真实自定义口令仍以环境变量为准。
    values = {
        **os.environ,
        "MYSQL_PASSWORD": os.environ.get("MYSQL_PASSWORD") or "fpa_dev_password",
    }
    connection = pymysql.connect(**connection_config(values).as_kwargs())
    removed_partners = removed_areas = 0
    try:
        with connection.cursor() as cursor:
            for prefix in _PARTNER_PREFIXES:
                cursor.execute(
                    "DELETE bp FROM business_partners AS bp "
                    "WHERE bp.code LIKE %s "
                    "  AND NOT EXISTS (SELECT 1 FROM purchase_orders AS o WHERE o.supplier_id = bp.id) "
                    "  AND NOT EXISTS (SELECT 1 FROM sales_orders AS s WHERE s.customer_id = bp.id)",
                    (prefix,),
                )
                removed_partners += cursor.rowcount
            for prefix in _AREA_PREFIXES:
                cursor.execute(
                    "DELETE a FROM areas AS a "
                    "WHERE a.code LIKE %s "
                    "  AND NOT EXISTS (SELECT 1 FROM ponds AS p WHERE p.area_id = a.id)",
                    (prefix,),
                )
                removed_areas += cursor.rowcount
        connection.commit()
    finally:
        connection.close()
    print(
        f"[cleanup_ui_probes] 已清理 UI E2E 探针：往来单位 {removed_partners} 条、区域 {removed_areas} 条"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
