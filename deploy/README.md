# 这个目录**是空的**，生产部署产物还没做

不是漏提交 —— 是**有意留下的欠账**：

| 欠账 | 现状 |
|---|---|
| Dockerfile / docker-compose | 没有 |
| Nginx 配置（静态资源托管 + 反代） | 没有 |
| CI 流水线 | 没有（门禁靠手工跑，命令在 `README.md` 的「验证」一节） |
| 备份 / 回滚脚本 | 没有（迁移只有前进，没有 down） |

## 现在怎么跑

**唯一被真实验证过的形态是「本地开发」**：

```powershell
python tools\serve_dev.py 5101     # 后端（waitress，Windows 上 gunicorn 因 fcntl 不可用）
cd frontend; npm run dev           # 前端（Vite，默认 5273，反代 /api → 5101）
```

细节、依赖清单、上线前验证、回滚口径：**见 `docs/DEPLOY.md`**。

## 落地这个目录时该放什么

一个自洽的部署包应该至少包含：`compose.yml`（或 `Dockerfile` + 启动脚本）、
Nginx 站点配置、环境变量清单（`.env.example` 的**生产版**，且**不含任何密钥值**）、
以及一份"怎么验证部署成功"的命令清单（就是 `README.md` 里那几条）。
