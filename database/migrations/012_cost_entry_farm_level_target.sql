-- 让成本记录能归集到「基地」层级：`cost_entries.area_id` 改为可空。
--
-- 背景（实测缺陷）：registry §2.10 允许 `target_type` ∈ {farm, area, pond, batch}，
-- 但 `cost_entries.area_id` 是 NOT NULL，而目标表里有两张**没有"区域"这一层**：
--   * `farms`  只有 organization_id（基地自己就是基地，不属于任何区域）；
--   * `areas`  只有 organization_id / farm_id（区域自己就是区域）。
-- 于是 `_resolve_tenant` 里那条
--     SELECT organization_id, farm_id, area_id FROM <目标表> WHERE <主键> = %s
-- 对 farm / area 必然抛 MySQL 1054「未知列」，再被
-- `kernel/uow.py::_translate_db_error` 兜底成
--     503 SERVICE_UNAVAILABLE 数据库服务暂时不可用
-- —— 用户被告知"数据库坏了"，真实原因是"这个归属层级在表结构上取不出来"。
-- （4 个 target_type 里 farm / area 提交必失败，pond / batch 正常。）
--
-- 改完之后的语义约定：
--   target_type=farm        -> organization_id / farm_id 有值，area_id = NULL
--   target_type=area        -> 三个键都有值（area_id = 该区域自己的 id）
--   target_type=pond/batch  -> 三个键都有值（从目标对象上取）
-- 配套的代码改动在 `backend/yuxin/domains/cost/entries_write.py::_resolve_tenant`。
--
-- 为什么"可空"而不是"填一个区域"：基地级成本不属于任何单个区域，
-- 从该基地下随便挑一个区域填进去是**静默错归属**（这正是本项目一贯要根除的形态）。
--
-- 影响面（逐条核过）：
--   * DataScope：`cost.entry.list` / `cost.summary` 用 `resource(area_id)` 谓词，
--     NULL 不匹配任何区域范围，但 `resource(farm_id)` 谓词会命中
--     —— 即"基地级成本对基地级范围可见"，与语义相符。
--   * 索引 `idx_cost_entries_scope_period (organization_id, farm_id, area_id, occurred_on)`
--     与 InnoDB 二级索引都允许 NULL，不需要重建。
--   * 外键 `fk_cost_entries_area` 允许 NULL（MySQL 语义：NULL 不参与外键校验）。
--   * `uq_cost_entries_org_dedupe` 的生成列**不含 area_id**，不受影响。
SET NAMES utf8mb4;

ALTER TABLE cost_entries
  MODIFY COLUMN area_id BIGINT UNSIGNED NULL;
