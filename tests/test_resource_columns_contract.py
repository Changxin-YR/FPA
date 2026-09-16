"""守卫：`Resource.columns` 里**不得出现重复的列标签**。

## 为什么需要它（一次真实的"中英文混杂"）

物料的 `Resource` 原先同时声明了两列、**标签都是「记录状态」**：

    ("status", "记录状态"),        # 机器码：verified / draft …
    ("status_label", "记录状态"),   # 中文：已核验 / 草稿 …

于是详情页渲染出**两行同名栏目**，一行中文一行英文。用户看到的第一反应是
"系统中英文混杂"，而不是"这里有两列"——因为**同名让人无法区分哪列是哪列**。

全系统的约定是：机器码列叫 `状态码`、展示列叫 `状态`（其余 10 个资源都这么写），
`docs/DISPLAY_CONTRACT.md` 也把这条写成"给人看的是 `*_label` 派生列"。
本用例把这条约定钉成**类级**判据：不管哪个资源、哪两列，标签重复即红。
"""

from __future__ import annotations

from collections import Counter

from conftest import load_all_status


def test_资源列标签不得重复():
    load_all_status()
    from yuxin.kernel.workflow import RESOURCES

    resources = RESOURCES.all() if hasattr(RESOURCES, "all") else list(RESOURCES)
    # 空集不得当成通过
    assert len(resources) > 10, f"只读到 {len(resources)} 个资源，判据可能失效"

    problems: list[str] = []
    for res in resources:
        labels = [label for _key, label in res.columns]
        dups = [lab for lab, n in Counter(labels).items() if n > 1]
        if dups:
            problems.append(f"{res.name}: 重复标签 {dups}（列={labels}）")

    assert not problems, (
        "这些资源的 columns 里出现了重复标签 —— 界面上会出现两列同名、无法区分"
        "（实测物料详情因此渲染出一中文一英文两行「记录状态」）：\n  "
        + "\n  ".join(problems)
    )
