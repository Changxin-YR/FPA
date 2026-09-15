# 显示契约（跨端 JSON 里不许出现用户读不懂的形态）

> 守它的测试：`tests/test_wire_format.py`（单元级）+ `tests/test_wire_format_e2e.py`（真链路）。
> 本文是该契约的**散文版**；判据以那两条测试为准，两者不一致时改本文。

## 为什么要有这份契约（一次真实的用户报障）

用户说"尤其是中英文混杂"。实测在**同一个响应**里同时命中三处：

```
sold_at       = 'Wed, 02 Sep 2026 00:00:00 GMT'   ← 日期成了 HTTP 头的格式
total_amount  = '100.0000000'                       ← 金额 7 位小数
unit          = 'jin'                               ← 计量单位是英文码
```

三处的**根因各不相同**，所以修法也不同：

| 症状 | 根因 | 修法（落点） |
|---|---|---|
| GMT 日期 | Flask `DefaultJSONProvider` 对 `datetime` / `date` 用 RFC 1123 | 换 JSON provider，**一处**：`web/app.py::_install_wire_format` |
| 7 位小数 | `数量(16,3) × 单价(14,4)` 的**派生值**没有列定义兜住 | 按金额口径量化：`kernel/money.py` |
| `jin` | 码是 `CHECK` 约束的合法值，**缺的是展示标签** | 内核 `UNIT_LABELS` + 行内派生 `unit_label` |

**为什么必须写成断言、而不是"修完就算"**：这三处**都不会报错** ——
日期是合法字符串、7 位小数是合法数字、`jin` 是合法枚举。它们只是**对人不可读**，
所以没有任何一层会拦住回归。

## 契约条款

### 1. 日期与时刻

* `date` 列 → `YYYY-MM-DD`；`datetime` 列 → ISO 8601（含 `T`）。
* **响应里不得出现 `GMT` / RFC 1123 形态**（那是 HTTP 头的格式，不是给人看的）。
* 前端也不得自己再格式化一遍：日期形状由服务端决定，前端只渲染。

### 2. 小数位

| 语义 | 位数 | 常量 |
|---|---|---|
| 金额（含**派生**金额，如 `数量 × 单价`） | **2** | `kernel/money.py::MONEY_QUANTUM` |
| 单价 | **4** | `UNIT_PRICE_QUANTUM` —— 金额的"2 位"规则**不适用**于单价，别被"统一截成 2 位"误伤 |
| 数量 | **3** | `QUANTITY_QUANTUM` |

输出统一走 `kernel/money.py::as_str()`：按 `Decimal` 自身位数输出字符串，
**不引科学计数法、不吞尾零**。浮点永远不参与金额计算。

### 3. 计量单位

* 行里的 `unit` 是**机器码**（`kg` / `jin` / `bag` …），**必须保持原样**——
  改码会打断 DB 的 `CHECK` 约束与 `HarvestQuantityMatch` 不变量。
* 给人看的是派生列 `unit_label`（词表在内核 `UNIT_LABELS`）。
* 物料分类同理：`category` 是码（`feed`），展示用 `category_label`（词表在 `kernel/workflow.py::CATEGORY_LABELS`）。

### 4. 中文文案的唯一来源（与本契约同源的一条纪律）

| 文案 | 唯一定义处 |
|---|---|
| 状态中文 | 状态机的 `State.label`（经 `/meta/capabilities` 的 `status_dict` 下发） |
| 行内动作中文 | `kernel/workflow.py::ACTION_LABELS`（经 `actions.row_action_labels` 下发） |
| 域 / 模块名 | `kernel/workflow.py::MODULE_LABELS` |
| 计量单位 / 分类 | `UNIT_LABELS` / `CATEGORY_LABELS` |

**前端不得持有任何业务文案映射表。** 未登记的码回退成原词（可诊断），
不静默换成别的中文 —— 早期版本正是让同一状态在 14 处各自翻译
（`verified` 在成本页叫「待确认」、在别的页叫「已核验」），才有的这条口径。

## 怎么复核

```powershell
python -m pytest tests/test_wire_format.py tests/test_wire_format_e2e.py -q
```

真链路那一条会登录 `demo` 并遍历**每个资源**的列表响应，逐行检查上面三条
（它不是抽查：`test_wire_format_e2e.py` 会按 `/meta/capabilities` 里的资源清单逐个打）。
