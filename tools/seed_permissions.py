"""从 REGISTRY 派生权限码种子（t17）。

## 问题

`permissions` 表是权限判定链路的**末端**：

    sessions -> users -> user_roles -> roles
                     -> role_permissions -> permissions.code

`AccessService._load()` 用这条链算出用户权限集合；表里没有该 code，集合里就没有它，
于是**任何需要权限的能力都会以「权限不足」失败**——而报错文案看起来像配置问题，
不像"字典没种"。

## 为什么必须是"派生"而不是一张手写的权限清单

实测（`tools/cost/survey_permission_codes.py`）：

```
能力 62 条；权限码 47 个
一个码被多条能力共用： 13 个（如 cost.view <- cost.entry.list + cost.summary）
码与能力名不同名的：   34 个（如 cost.manage <- cost.entry.create）
```

也就是说**大多数权限码并不等于能力名**（`cost.manage` / `sales.deliver` /
`warehouse.receipt.verify` / `finance.payment.manage` …）。手写清单在这里不是"容易漏"，
而是**必然错**——而且错了不会报错，只会在某个角色配不出权限时表现为"点了没反应"。
所以本工具的唯一输入是 `yuxin.bootstrap.load_all()` 的注册表；**没有任何手抄常量表**。

## 三个设计问题的判断（任务说明要求给理由，不照做）

**1. 只种权限字典，还是连带种角色与授权？—— 只种字典。**

  权限码是**派生量**：`Capability.required_permission` 是权威声明，码从它机械导出。
  派生量不同步就是缺陷，所以"种进数据库"这件事本身是修复。

  而"哪个角色该有哪些权限"是**组织决策**，注册表里没有它。把某个角色-权限组合
  硬编码进种子，等于造出**第三处权限真相**（另外两处是 `Capability.required_permission`
  与真库里的 `role_permissions`）——正是本项目要根除的形态。生产上这一步应当由
  管理员经页面完成，而不是由一次迁移替他决定。

  所以脚本提供 `--demo-role` 开关：只在**验收/自检**时种一个演示角色来证明链路通了，
  默认**不种**。演示数据与产品数据分开，是为了让"生产库里有没有被塞进一个手写角色"
  成为一个可检查的事实，而不是一次默认行为。

**2. `required_permission=None` 怎么办？—— 不种，且显式报告为 0。**

  `None` 的语义是"**无需权限，仅需登录**"（registry §1.1 的 `auth.login` 一类），
  它**没有**权限码可种——为它编一个码，等于凭空发明一条不存在的权限，
  然后任何人配角色时都会看到它。所以正确的映射是"无码即无行"。

  但本版实测是 **0 条**（62 条能力全部有权限码），所以这条规则当前不会被触发。
  脚本仍会打印这个数量：**从 0 变成非 0 是一个需要被看见的变化**（说明新域引入了
  免权限能力），而不是一个静默的分支。

**3. 放迁移还是放 tools？—— 放 `tools/`，且给出可机械执行的理由。**

  迁移是纯 SQL，**读不到 Python 注册表**。要种这 47 个码，就得在迁移里手写一份
  清单——那就回到了上面第 1 点否定掉的东西：一份会与 `Capability` 声明漂移的副本。
  **派生工具放 `tools/`，因为它必须能 import `yuxin.bootstrap`。**

  代价是"种子不会随 `migrate.py apply` 自动执行"。这个代价用一个 **`--check` 模式**
  补上：它可以放进任何门禁/自检，一旦注册表新增了未种的权限码就报错退出。
  这样"忘了跑"从静默失败变成显式失败——与内核那边"缺上下文必须报错"是同一条原则。

## 用法

    python tools/seed_permissions.py              # 种（幂等）
    python tools/seed_permissions.py --check      # 只核对，缺码则 exit 1（给门禁用）
    python tools/seed_permissions.py --dry-run    # 只打印将要写什么
    python tools/seed_permissions.py --demo-role  # 额外种一个演示角色（验收用）
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from yuxin.kernel.errors import DomainError  # noqa: E402
from yuxin.kernel.uow import ConnectionConfig, UnitOfWork, translate_mysql_error  # noqa: E402

#: 演示角色的固定标识。用固定值而不是时间戳/随机：让"重复跑"幂等，
#: 也让"生产库里有没有这条"变成一个可以用一条 SELECT 回答的问题。
DEMO_ROLE_CODE = "yuxin-demo-all-permissions"
DEMO_ROLE_NAME = "演示：全部权限（自检/验收专用）"


class PermissionSeed:
    """一个权限码的派生结果。"""

    __slots__ = ("code", "domain", "name", "capabilities")

    def __init__(self, code: str, domain: str, name: str, capabilities: list[str]) -> None:
        self.code = code
        self.domain = domain
        self.name = name
        self.capabilities = capabilities


def derive() -> tuple[list[PermissionSeed], list[str], list[str], list[str]]:
    """从真实注册表派生权限码。

    返回 `(种子列表, 无语权限码的能力, 一致性告警, 装载失败的域)`。

    为什么把"装载失败的域"单独返回而不是塞进告警：**它会让派生结果不完整**，
    而"不完整的派生"与"完整的派生"在只看 `缺了几个` 时是分不清的——
    域装载失败时它们的权限码根本不在结果里，于是 `缺=0` 看起来像"全都种好了"。
    这是一个**假成功**，必须能被调用方区分出来。实测发生过一次：
    `sales` 装载失败 → 派生从 47 个降到 39 个 → 若不报出来，就是静默漏掉 8 个码。
    """
    import importlib

    from yuxin.bootstrap import _module_name, discover_domains
    from yuxin.kernel.capability import REGISTRY

    failed_domains: list[str] = []
    for domain in discover_domains():
        try:
            importlib.import_module(_module_name(domain))
        except Exception as exc:  # noqa: BLE001 - 记下来，不静默跳过
            failed_domains.append(f"{domain}: {type(exc).__name__}: {exc}")

    by_code: dict[str, list[object]] = defaultdict(list)
    no_permission: list[str] = []
    for capability in REGISTRY.all():
        if capability.required_permission is None:
            no_permission.append(capability.name)
        else:
            by_code[capability.required_permission].append(capability)

    warnings: list[str] = []
    seeds: list[PermissionSeed] = []
    for code, capabilities in sorted(by_code.items()):
        domains = sorted({c.domain for c in capabilities})  # type: ignore[attr-defined]
        if len(domains) > 1:
            warnings.append(f"权限码 {code} 被跨域共用：{domains}")
        # 取中文名称的**优先级**（都是确定性的，可复现）：
        #
        #   1. **能力名与权限码完全相同的**那条 —— 它的标题就是这个码最贴切的名字。
        #      例：`batch.view` 由 `batch.list` 提供，若只按字母序取会得到
        #      "提交批次核验"（`batch.update` 的标题），而那是**另一个码**的含义。
        #   2. 否则取能力名**字母序最靠前**的那条 —— 不能用"最后一次声明的那个"，
        #      那会随导入顺序变化而漂移。
        exact = [c for c in capabilities if c.name == code]  # type: ignore[attr-defined]
        pool = exact or capabilities
        representative = sorted(pool, key=lambda c: c.name)[0]  # type: ignore[attr-defined]
        seeds.append(
            PermissionSeed(
                code=code,
                domain=domains[0] if domains else "",
                name=representative.title,  # type: ignore[attr-defined]
                capabilities=sorted(c.name for c in capabilities),  # type: ignore[attr-defined]
            )
        )
    return seeds, no_permission, warnings, failed_domains


def connection_config() -> ConnectionConfig:
    """与 `tools/preflight.py` / 内核 `uow_factory` 一致：都从环境变量取。

    不自己拼默认值——默认值只应有一处（内核 `uow_factory._DEFAULTS`）。
    """
    from yuxin.kernel.uow_factory import connection_config as kernel_config

    return kernel_config()


def existing_codes(uow: UnitOfWork) -> dict[str, dict]:
    with uow.begin() as tx:
        rows = tx.query_all("SELECT id, code, name, domain FROM permissions")
        return {str(row["code"]): row for row in rows}


def apply_seeds(seeds: list[PermissionSeed]) -> tuple[int, int]:
    """幂等写入。返回 `(新增, 更新)`。

    用 `INSERT ... ON DUPLICATE KEY UPDATE`：`uq_permissions_code` 是唯一键，
    所以重复跑只会更新 `name`/`domain`（派生值本来就该跟着注册表走）。
    **不 DELETE 任何行**：库里可能有管理员手工建的码，删掉就等于替他做决定。
    """
    uow = UnitOfWork(connection_config())
    inserted = updated = 0
    with uow.begin() as tx:
        before = {str(r["code"]) for r in tx.query_all("SELECT code FROM permissions")}
        for seed in seeds:
            tx.execute(
                "INSERT INTO permissions (code, name, domain, description) "
                "VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), domain=VALUES(domain)",
                (
                    seed.code,
                    seed.name,
                    seed.domain,
                    "由 backend/yuxin/domains/*/capabilities.py 派生（tools/seed_permissions.py）",
                ),
            )
            if seed.code in before:
                updated += 1
            else:
                inserted += 1
    return inserted, updated


def apply_demo_role(seeds: list[PermissionSeed]) -> tuple[int, int]:
    """种一个**演示**角色并把全部权限授给它。返回 `(角色id, 授权条数)`。

    为什么允许它存在：验收要求"同一个角色/用户跑一条真实能力，种之前失败、种之后成功"，
    而那需要一个持有权限的角色。它被刻意命名为 `yuxin-demo-*`，以便生产库清理时
    一条 `DELETE FROM roles WHERE code LIKE 'yuxin-demo-%'` 就能摘掉。
    """
    uow = UnitOfWork(connection_config())
    with uow.begin() as tx:
        tx.execute(
            "INSERT INTO roles (code, name, description) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)",
            (DEMO_ROLE_CODE, DEMO_ROLE_NAME, "自检/验收专用：持有全部派生权限"),
        )
        role = tx.query_one("SELECT id FROM roles WHERE code=%s", (DEMO_ROLE_CODE,))
        role_id = int(role["id"])
        granted = 0
        for seed in seeds:
            row = tx.query_one("SELECT id FROM permissions WHERE code=%s", (seed.code,))
            if row is None:
                continue
            tx.execute(
                "INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE role_id=VALUES(role_id)",
                (role_id, int(row["id"])),
            )
            granted += 1
    return role_id, granted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 REGISTRY 派生并种权限码")
    parser.add_argument("--check", action="store_true",
                        help="只核对：注册表里有、库里没有的码会列出来并 exit 1")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    parser.add_argument("--demo-role", action="store_true",
                        help="额外种一个演示角色并授予全部权限（自检/验收专用）")
    args = parser.parse_args(argv)

    seeds, no_permission, warnings, failed_domains = derive()
    print(f"派生结果：权限码 {len(seeds)} 个；required_permission=None 的能力 "
          f"{len(no_permission)} 条")
    if no_permission:
        print(f"  （None 的语义是「无需权限、仅需登录」，因此**没有码可种**）："
              f"{no_permission[:8]}")
    for warning in warnings:
        print(f"  警告：{warning}")
    if failed_domains:
        # 这不是"警告"，是"本次派生结果不完整" —— 必须能被区分出来
        print(f"  装载失败的域（它们的权限码**不在**本次派生结果里）：")
        for item in failed_domains:
            print(f"      ! {item}")

    # ★ 空输入不是"通过"，是"没检查"
    if not seeds:
        print("\nFAIL  一个权限码都没派生出来 —— 注册表没装载成功？")
        print("      空输入不是通过。")
        return 1

    try:
        known = existing_codes(UnitOfWork(connection_config()))
    except Exception as exc:  # noqa: BLE001
        translated = translate_mysql_error(exc) if isinstance(exc, Exception) else None
        print(f"\nFAIL  连不上数据库：{translated or exc}")
        return 2

    missing = [s.code for s in seeds if s.code not in known]
    extra = sorted(set(known) - {s.code for s in seeds})

    print(f"\n库中已有权限码 {len(known)} 个；派生 {len(seeds)} 个")
    print(f"  缺（注册表有、库里没有）：{len(missing)}")
    for code in missing:
        print(f"      - {code}")
    if extra:
        # 不删，但要报出来：多出来的码可能是管理员手工建的，也可能是**已删除能力**的残留。
        print(f"  多（库里有、注册表没有）：{len(extra)}")
        for code in extra:
            print(f"      + {code}")

    if args.check:
        if failed_domains:
            print(f"\nFAIL  {len(failed_domains)} 个域装载失败 —— 本次派生结果**不完整**，"
                  "`缺=0` 不代表已种齐（它们的权限码根本没被派生出来）")
            print("      先修装载，再跑本检查。")
            return 1
        if missing:
            print(f"\nFAIL  {len(missing)} 个权限码未入库 —— 那些能力在真实链路上会"
                  "以「权限不足」失败（而文案看起来像配置问题）")
            print("      修法：python tools/seed_permissions.py")
            return 1
        print("\nOK  注册表里的权限码已全部入库")
        return 0

    if args.dry_run:
        print("\n（--dry-run：未写库）")
        return 0

    inserted, updated = apply_seeds(seeds)
    print(f"\n已写入：新增 {inserted}，更新 {updated}（幂等；未删除任何行）")

    if args.demo_role:
        role_id, granted = apply_demo_role(seeds)
        print(f"已种演示角色 {DEMO_ROLE_CODE}(id={role_id})，授予 {granted} 个权限")
        print("  提醒：这是**验收用**数据，生产库应用 "
              "`DELETE FROM roles WHERE code LIKE 'yuxin-demo-%'` 摘掉")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
