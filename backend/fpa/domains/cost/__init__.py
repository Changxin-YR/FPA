"""成本域（cost）：成本归集、汇总、关账。

模块划分与 master_data 同构（DEVELOPMENT.md §3 的固定七步）：

    service.py        状态机 + 资源声明 + 共享校验与派生
    entries.py        读路径（列表 / 详情 / 回读 / 汇总）
    entries_write.py  写路径（登记 / 提交 / 确认 / 归档 / 关账）
    capabilities.py   能力声明（5 条，派生路由 / 权限 / 范围 / 幂等 / 确认 / 不变量）

组合根 `fpa.bootstrap` 按目录自动发现 `capabilities.py`，因此**新增本域不需要修改
任何共享文件**——这正是让五个域能并行开工、而不在同一个 import 列表上互相冲突的前提。
"""
