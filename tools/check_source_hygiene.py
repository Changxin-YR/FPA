"""源码卫生检查：把"手工编辑引入的不可见损伤"拦在提交之前。

**为什么需要这个工具**：本项目已经**三次**因为同一类原因受损，而且每次都是
"文件看起来正常、编辑器不报错、只有特定工具才会失败"：

    ① PowerShell 5.1 的 `Set-Content -Encoding UTF8` / `Out-File -Encoding UTF8`
       **会写入 BOM**。MySQL 解析 `.sql` 时报
       `syntax error near '\\ufeff-- ===='`（已在迁移 001 上真实发生）；
    ② PowerShell 写文件时还可能吃掉前导字符——实测 `/** ... r` 被写成 `* ...`，
       导致 JSDoc 未闭合、注释泄漏进代码、TypeScript 报
       `Cannot find name 'meta'`（已在 `DynamicForm.vue` 上真实发生）；
    ③ `.gitattributes` 未固定 `*.sql`，导致 7/35 个迁移文件是 CRLF，
       早期版本四份 runner 因此互相判"校验和漂移"。

所以本工具检查的是**文件本身的物理属性**，不是代码逻辑：

    * 不含 BOM
    * 是合法 UTF-8（无替换字符 U+FFFD）
    * 不含 CRLF（统一 LF——这样 `.gitattributes` 就不再是唯一的保障）
    * 末行有换行（POSIX 惯例，也让 diff 更干净）

用法::

    python tools/check_source_hygiene.py            # 全量检查
    python tools/check_source_hygiene.py a.py b.ts  # 只检查指定文件（供 pre-commit 用）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEXT_SUFFIXES = {
    ".py", ".ts", ".vue", ".js", ".mjs", ".cjs", ".json", ".sql",
    ".md", ".yml", ".yaml", ".css", ".html", ".sh",
}

#: 要求 CRLF 的后缀。Windows 批处理文件用 LF 会在标签跳转等场景下解析异常，
#: 所以这里不是"放行例外"，而是"该格式本就要求 CRLF"。
CRLF_REQUIRED_SUFFIXES = {".cmd", ".bat"}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "dist", "release", "coverage",
    "htmlcov", "test-results", "playwright-report", ".pytest_cache",
    ".ruff_cache", ".dsh-home", ".vite", "blob-report",
    # 多人协作（当前迭代施工用的多智能体协作工具）的**会话状态目录**。
    #
    # 为什么它可以进 SKIP_DIRS，而 `.tmp-*/` 刻意不能：`SKIP_DIRS` 的本意是
    # "别让**临时产物泄进仓库**"（那些文件是**我们**写的，格式由我们负责，扫它们有意义）。
    # 而 `.worklog/team.json` 是**插件**写的、每次落盘都不带末行换行 ——
    # 我们无法通过纪律修好它，扫它只会产生一条**永远无法消除的假红**，
    # 把真正的卫生问题淹没掉（这正是本文件其它注释反复强调的失败形态）。
    # 配套：`.gitignore` 里也加了 `.worklog/`，所以它进不了仓库。
    ".worklog",
}

#: 明确豁免的文件（附理由）。空集是理想状态——每加一条都要写下为什么。
EXEMPT: dict[str, str] = {}


def walk(paths: list[str] | None) -> list[Path]:
    """收集待检查的文件。

    **显式路径可以是文件也可以是目录。** 初版只处理文件（`is_file()` 判断），
    传目录时静默返回空列表——`check_source_hygiene.py agent-runtime` 会打印
    "检查通过：0 个文件"，**看着通过实际什么都没查**。

    这类"空输入当成通过"的假阳性比报错危险得多，因为它让人以为已经验证过了。
    现在的行为：目录会被递归展开；路径不存在则**抛错**，而不是当成空集。
    """
    if paths:
        collected: list[Path] = []
        for item in paths:
            target = Path(item).resolve()
            if target.is_file():
                collected.append(target)
                continue
            if target.is_dir():
                # 目录分支也要遵守同一套过滤规则，否则传 `.` 会把 node_modules 扫进来。
                # 目录分支同样用剪枝——传 `.` 时不能进入 node_modules / .dsh-home。
                for directory, dirnames, filenames in os.walk(target):
                    dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
                    for filename in filenames:
                        candidate = Path(directory) / filename
                        if candidate.suffix.lower() in TEXT_SUFFIXES:
                            collected.append(candidate)
                continue
            raise SystemExit(
                f"路径不存在：{item}（既不是文件也不是目录）。"
                "注意：不存在的路径不会被当成空集——那会掩盖拼错的路径。"
            )
        if not collected:
            raise SystemExit(
                "指定的路径下没有找到任何可检查的文本文件。"
                "这通常意味着路径写错了或后缀不在检查范围内，"
                "而不是\"检查通过\"。"
            )
        return sorted(set(collected))

    found: list[Path] = []
    # 用 os.walk 并在 dirnames 上**原地剪枝**，而不是 rglob + 事后过滤。
    #
    # `Path.rglob` 无法剪枝——它一定会进入每个子目录。仓库里有 `.dsh-home/`
    # （Harness 运行时闭包，193 个 npm 包），rglob 会进入它再逐个过滤，
    # 实测遍历 4.3 万项、耗时 25 秒以上。剪枝后是毫秒级。
    #
    # 这个差别会随仓库长大而放大，且症状是"检查变慢"而不是"检查出错"，
    # 很容易被当成机器慢而忽略。
    for directory, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            candidate = Path(directory) / filename
            if candidate.suffix.lower() in TEXT_SUFFIXES:
                found.append(candidate)
    if not found:
        raise SystemExit("仓库里没有找到任何可检查的文本文件，检查配置可能有问题。")
    return sorted(found)


def check(path: Path) -> list[str]:
    problems: list[str] = []
    rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)

    if EXEMPT.get(rel):
        return problems

    raw = path.read_bytes()
    if not raw:
        return problems  # 空文件由各语言自己的工具管，不归本检查

    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(
            f"{rel}: 含 UTF-8 BOM（EF BB BF）。MySQL 会把它当 SQL 语法错误，"
            "其他工具也可能因此解析失败。"
        )
        raw = raw[3:]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        problems.append(f"{rel}: 不是合法 UTF-8（{exc}）")
        return problems

    if "\ufffd" in text:
        problems.append(
            f"{rel}: 含 U+FFFD 替换字符，说明写入时编码转换丢过数据"
        )

    requires_crlf = path.suffix.lower() in CRLF_REQUIRED_SUFFIXES
    has_crlf = b"\r\n" in raw

    if requires_crlf and not has_crlf:
        problems.append(
            f"{rel}: Windows 批处理文件必须使用 CRLF 换行"
            "（LF 会让标签跳转等语法解析异常）。"
        )
    elif not requires_crlf and has_crlf:
        crlf = raw.count(b"\r\n")
        problems.append(
            f"{rel}: 含 {crlf} 处 CRLF。请统一为 LF——"
            "早期版本正因 `.gitattributes` 未固定 `*.sql`，导致 7/35 个迁移是 CRLF，"
            "四份 runner 互相判校验和漂移。"
        )
    elif not requires_crlf and b"\r" in raw:
        problems.append(f"{rel}: 含孤立 CR")

    # 末行换行：不阻塞，但列出来便于统一
    if not text.endswith("\n"):
        problems.append(f"{rel}: 末行缺少换行符")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查源码文件的物理卫生")
    parser.add_argument("paths", nargs="*", help="只检查指定文件")
    args = parser.parse_args(argv)

    files = walk(args.paths or None)
    all_problems: list[str] = []
    for path in files:
        all_problems.extend(check(path))

    if not all_problems:
        print(f"卫生检查通过：{len(files)} 个文本文件（无 BOM / 合法 UTF-8 / 统一 LF / 末行换行）")
        return 0

    print(f"卫生检查失败：{len(files)} 个文件中有 {len(all_problems)} 处问题\n")
    for problem in all_problems:
        print(f"  {problem}")
    print()
    print("修复方式（PowerShell 正确写法）：")
    print("  $b = [IO.File]::ReadAllBytes($p)")
    print("  $t = [Text.Encoding]::UTF8.GetString($b) -replace \"`r`n\", \"`n\"")
    print("  $t = $t.TrimStart([char]0xFEFF)          # 去 BOM")
    print("  [IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($false)))")
    return 1


if __name__ == "__main__":
    sys.exit(main())
