-- 交付单补充计量单位，和销售单保持一致。
-- 旧数据默认按公斤兼容；新数据由交付表单选择并由服务校验必须匹配销售单。
ALTER TABLE deliveries
  ADD COLUMN unit VARCHAR(16) NOT NULL DEFAULT 'kg' AFTER pond_id,
  ADD CONSTRAINT chk_deliveries_unit CHECK (unit IN ('kg', 'jin', 'tail'));
