# 从上一代数据库迁移到 `yuxin`

> 本页出现的 `adp_new`、`/fpa/` 等是**上一代部署的真实标识**（老库名、老站点路径），
> 不是本项目现在的名字——搬迁命令必须照服务器上的真名写。反向引用见 [DEPLOY.md](DEPLOY.md)。

一次性运维步骤，只在「库里已有数据、不想重建」时需要。

```powershell
$env:MYSQL_ROOT_PASSWORD='<root 密码>'
python tools\bootstrap_db.py            # 建 yuxin 库 + yuxin 账号 + 授权（幂等）

$mysql = 'C:\Program Files\MySQL\MySQL Server 9.7\bin'
cmd /c "`"$mysql\mysqldump.exe`" -u root -p<root 密码> --single-transaction --routines --triggers --events --set-gtid-purged=OFF adp_new > old.sql"
cmd /c "`"$mysql\mysql.exe`" -u root -p<root 密码> --default-character-set=utf8mb4 yuxin < old.sql"
```

### 坑 1：导入后必须重建审计触发器

`mysqldump` 会把 `DEFINER=<旧账号>@127.0.0.1` 一并带过来，而旧账号在新库上没有
`TRIGGER` 权限 —— 应用账号更新 `audit_logs` 会报
`1142 TRIGGER command denied to user '<旧账号>'`，表现是
`tools/db_selfcheck.py` 的两条审计断言变红。
**以应用账号（`yuxin`）重建即可**，DDL 见 `database/migrations/001_identity_access_governance.sql`：

```sql
DROP TRIGGER IF EXISTS audit_logs_no_update;
DROP TRIGGER IF EXISTS audit_logs_no_delete;
CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only';
CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only';
```

### 坑 2：`migrate.py verify` 的 checksum 漂移是既有问题

`python tools\migrate.py verify` 会对 10 个迁移报「应用后被修改过」。这是**迁移之前就存在**的：
老库建于 2026-09-15，此后迁移文件被编辑过（已用 `git show <改名前的 commit>` 逐个 A/B 证实，
与本次重命名无关）。它不影响 `migrate.py apply` 与运行时；要彻底消除需重建库或重新登记
`schema_migrations.checksum`。

---
