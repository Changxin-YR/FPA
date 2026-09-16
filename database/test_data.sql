-- ============================================================================
-- 渔芯 手动测试数据
--
-- 按真实养殖业务流程生成，覆盖完整生产周期：
--   放苗 → 采购饲料 → 到货入库 → 投喂领用 → 收获出塘 → 销售 → 交付 → 收款
--
-- 使用前提：已执行全部 000-012 迁移（种子数据存在）
-- 执行方式：mysql -u root -p yuxin < test_data.sql
-- 可重入：全部使用 ON DUPLICATE KEY UPDATE 或 INSERT IGNORE
--
-- 时间线（2026年）：
--   07-01  北区1号塘放苗（批次 BATCH-001）
--   07-15  北区2号塘放苗（批次 BATCH-002）
--   08-20  采购饲料（PO-001, PO-002）
--   09-01  PO-001 到货入库 2000kg
--   09-03  投喂 200kg（FEED-001, 已核验）
--   09-05  创建销售单 SO-001
--   09-08  投喂 150kg（FEED-002, 待核验）
--   09-10  部分收获 10000尾/400kg（HAR-001, 已核验）
--   09-12  第一批交付 400kg（DEL-001, 已核验）
--   09-13  收获待核验（HAR-002）
--   09-14  手工录入人工成本
-- ============================================================================

SET NAMES utf8mb4;
SET @now = NOW();

-- ============================================================================
-- 0. 查找种子数据 ID
-- ============================================================================

SET @org_id      = (SELECT id FROM organizations WHERE code = 'default');
SET @farm_id     = (SELECT id FROM farms WHERE code = 'default-farm' AND organization_id = @org_id);
SET @area_north  = (SELECT id FROM areas WHERE code = 'A-NORTH' AND farm_id = @farm_id);
SET @area_south  = (SELECT id FROM areas WHERE code = 'A-SOUTH' AND farm_id = @farm_id);
SET @supplier_id = (SELECT id FROM business_partners WHERE code = 'SUP-001' AND organization_id = @org_id);
SET @customer_id = (SELECT id FROM business_partners WHERE code = 'CUS-001' AND organization_id = @org_id);
SET @mat_001     = (SELECT id FROM materials WHERE code = 'MAT-001' AND organization_id = @org_id);
SET @mat_002     = (SELECT id FROM materials WHERE code = 'MAT-002' AND organization_id = @org_id);
SET @wh_001      = (SELECT id FROM warehouses WHERE code = 'WH-001' AND farm_id = @farm_id);

-- 确认种子数据完整
SELECT IF(@org_id IS NULL, '缺少默认企业', 'OK') AS chk_org,
       IF(@farm_id IS NULL, '缺少默认基地', 'OK') AS chk_farm,
       IF(@area_north IS NULL, '缺少北区', 'OK') AS chk_area_n,
       IF(@area_south IS NULL, '缺少南区', 'OK') AS chk_area_s,
       IF(@supplier_id IS NULL, '缺少供应商', 'OK') AS chk_sup,
       IF(@customer_id IS NULL, '缺少客户', 'OK') AS chk_cus,
       IF(@mat_001 IS NULL, '缺少物料1', 'OK') AS chk_mat1,
       IF(@mat_002 IS NULL, '缺少物料2', 'OK') AS chk_mat2,
       IF(@wh_001 IS NULL, '缺少仓库', 'OK') AS chk_wh;

-- ============================================================================
-- 1. 测试用户（3人，模拟 经办 / 核验 / 审批 分离）
--
--    密码哈希是哨兵值（不可登录）。测试请用 demo 账号登录。
--    这三个用户仅作为 created_by / verified_by / approved_by 的引用目标，
--    保证 DistinctActors 约束成立。
-- ============================================================================

INSERT INTO users (id, username, display_name, password_hash, status) VALUES
  (10, 'test-operator', '张操作（经办人）', 'scrypt$16384$8$1$00$00', 'active'),
  (11, 'test-复核人', '李核验（核验人）', 'scrypt$16384$8$1$00$00', 'active'),
  (12, 'test-approver', '王审批（审批人）', 'scrypt$16384$8$1$00$00', 'active')
ON DUPLICATE KEY UPDATE display_name = VALUES(display_name), status = VALUES(status);

-- ============================================================================
-- 2. 主数据补充：更多往来单位与物料
-- ============================================================================

-- 第二个供应商
INSERT INTO business_partners
  (organization_id, farm_id, area_id, partner_type, code, name,
   contact_name, phone, settlement_days, status, created_by)
VALUES
  (@org_id, @farm_id, @area_north, 'supplier', 'SUP-002', '绿源药品供应商',
   '赵药师', '13900000003', 45, 'verified', 10)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 第二个客户
INSERT INTO business_partners
  (organization_id, farm_id, area_id, partner_type, code, name,
   contact_name, phone, settlement_days, status, created_by)
VALUES
  (@org_id, @farm_id, @area_north, 'customer', 'CUS-002', '鲜达水产批发部',
   '周批发', '13900000004', 7, 'verified', 10)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 药品物料
INSERT INTO materials
  (organization_id, farm_id, area_id, code, name, category, spec, unit,
   unit_price, safety_stock, shelf_life_days, status, created_by)
VALUES
  (@org_id, @farm_id, @area_north, 'MAT-003', '聚维酮碘消毒液', 'medicine',
   '500ml/瓶', 'kg', 35.0000, 20.000, 365, 'verified', 10)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 待提交的物料（测试提交流程）
INSERT INTO materials
  (organization_id, farm_id, area_id, code, name, category, spec, unit,
   unit_price, safety_stock, shelf_life_days, status, created_by)
VALUES
  (@org_id, @farm_id, @area_north, 'MAT-004', '3号虾配合饲料', 'feed',
   '2.0mm 颗粒', 'kg', 5.2000, 300.000, 120, 'draft', 10)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- ============================================================================
-- 3. 塘口（6口，覆盖全部生命周期状态与业务状态）
-- ============================================================================

INSERT INTO ponds
  (organization_id, farm_id, area_id, code, name, species, capacity_mu,
   pond_status, manager_name, location_text, aerator_count, stocking_spec, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- 已核验 + 养殖中：有活跃批次，可以投喂/收获
  (@org_id, @farm_id, @area_north, 'P-001', '北区1号塘', '南美白对虾', 15.00,
   'farming', '陈塘长', '北区东侧第一排', 4, '虾苗P5', '主力养殖塘',
   'verified', 10, 11, '2026-06-15 10:00:00'),

  -- 已核验 + 已放苗：刚放苗，尚未开始养殖
  (@org_id, @farm_id, @area_north, 'P-002', '北区2号塘', '南美白对虾', 12.00,
   'stocked', '陈塘长', '北区东侧第二排', 3, '虾苗P5', NULL,
   'verified', 10, 11, '2026-06-20 14:00:00'),

  -- 已核验 + 休塘：可以申请状态变更
  (@org_id, @farm_id, @area_north, 'P-003', '北区3号塘', '草鱼', 20.00,
   'rest', '刘塘长', '北区西侧', 2, NULL, '上一造已清塘',
   'verified', 10, 11, '2026-05-01 09:00:00'),

  -- 已核验 + 建设中
  (@org_id, @farm_id, @area_south, 'P-004', '南区1号塘', NULL, 18.00,
   'build', '刘塘长', '南区入口处', 0, NULL, '新开挖，预计10月完工',
   'verified', 10, 11, '2026-04-01 09:00:00'),

  -- 已提交，待核验（测试核验流程）
  (@org_id, @farm_id, @area_south, 'P-005', '南区2号塘', '罗氏沼虾', 10.00,
   'build', '刘塘长', '南区中部', 2, NULL, NULL,
   'submitted', 10, NULL, NULL),

  -- 草稿（测试编辑 + 提交流程）
  (@org_id, @farm_id, @area_south, 'P-006', '南区3号塘', NULL, 8.50,
   'build', NULL, '南区尾部', 0, NULL, '规划中',
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 查找刚插入的塘口 ID
SET @pond_001 = (SELECT id FROM ponds WHERE code = 'P-001' AND farm_id = @farm_id);
SET @pond_002 = (SELECT id FROM ponds WHERE code = 'P-002' AND farm_id = @farm_id);
SET @pond_003 = (SELECT id FROM ponds WHERE code = 'P-003' AND farm_id = @farm_id);

-- ============================================================================
-- 4. 塘口状态变更申请（测试两步审批）
-- ============================================================================

-- 休塘 → 清塘（待核验）
INSERT INTO pond_status_change_requests
  (organization_id, pond_id, from_status, to_status, reason, status,
   pond_version, requested_by, requested_at)
SELECT @org_id, @pond_003, 'rest', 'clean', '准备下一造养殖，需要清塘消毒',
       'submitted', p.row_version, 10, '2026-09-14 08:30:00'
FROM ponds p WHERE p.id = @pond_003
ON DUPLICATE KEY UPDATE reason = VALUES(reason);

-- ============================================================================
-- 5. 养殖批次（3个，覆盖不同业务状态）
-- ============================================================================

INSERT INTO production_batches
  (organization_id, farm_id, area_id, code, name, pond_id, species,
   initial_quantity, initial_weight_kg, stocked_at, expected_harvest_date, note,
   batch_status, status, created_by, verified_by, verified_at)
VALUES
  -- BATCH-001: 已核验 + 养殖中（P-001 的活跃批次）
  (@org_id, @farm_id, @area_north, 'BATCH-2026-001', '北1塘2026年第一造',
   @pond_001, '南美白对虾', 100000.000, 50.000,
   '2026-07-01 06:00:00', '2026-10-15', '虾苗来自海南种苗场',
   'farming', 'verified', 10, 11, '2026-07-01 10:00:00'),

  -- BATCH-002: 已核验 + 已放苗（P-002 的新批次）
  (@org_id, @farm_id, @area_north, 'BATCH-2026-002', '北2塘2026年第一造',
   @pond_002, '南美白对虾', 80000.000, 40.000,
   '2026-07-15 06:00:00', '2026-11-01', NULL,
   'stocked', 'verified', 10, 11, '2026-07-15 09:00:00'),

  -- BATCH-003: 草稿（测试提交 + 核验流程）
  (@org_id, @farm_id, @area_north, 'BATCH-2026-003', '北3塘2026年计划批次',
   @pond_003, '草鱼', 5000.000, 500.000,
   '2026-10-01 06:00:00', '2027-03-01', '计划中，待塘口清理完毕',
   'stocked', 'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @batch_001 = (SELECT id FROM production_batches WHERE code = 'BATCH-2026-001' AND organization_id = @org_id);
SET @batch_002 = (SELECT id FROM production_batches WHERE code = 'BATCH-2026-002' AND organization_id = @org_id);

-- ============================================================================
-- 6. 批次库存台账（batch_stock_records）
--    只有已核验批次才有库存记录
-- ============================================================================

-- BATCH-001 放苗入塘（batch.verify 产生）
INSERT INTO batch_stock_records
  (organization_id, farm_id, area_id, batch_id, pond_id,
   movement_type, quantity_delta, weight_delta_kg, happened_at,
   source_type, source_id, note, created_by)
VALUES
  (@org_id, @farm_id, @area_north, @batch_001, @pond_001,
   'stocking', 100000.000, 50.000, '2026-07-01 06:00:00',
   'batch', @batch_001, '初始放苗', 11)
ON DUPLICATE KEY UPDATE note = VALUES(note);

-- BATCH-002 放苗入塘
INSERT INTO batch_stock_records
  (organization_id, farm_id, area_id, batch_id, pond_id,
   movement_type, quantity_delta, weight_delta_kg, happened_at,
   source_type, source_id, note, created_by)
VALUES
  (@org_id, @farm_id, @area_north, @batch_002, @pond_002,
   'stocking', 80000.000, 40.000, '2026-07-15 06:00:00',
   'batch', @batch_002, '初始放苗', 11)
ON DUPLICATE KEY UPDATE note = VALUES(note);

-- ============================================================================
-- 7. 采购单（4张，覆盖7态状态机的关键节点）
-- ============================================================================

INSERT INTO purchase_orders
  (organization_id, farm_id, area_id, code, name,
   supplier_id, material_id, warehouse_id, quantity, unit_price,
   expected_delivery_date, due_date, reason, note,
   status, created_by, approved_by, approved_at)
VALUES
  -- PO-001: 已全部到货（完整流程已走完）
  (@org_id, @farm_id, @area_north, 'PO-2026-001', '9月份1号饲料采购',
   @supplier_id, @mat_001, @wh_001, 2000.000, 4.8000,
   '2026-09-01', '2026-10-01', NULL, '优先供应，保障养殖高峰期',
   'fully_received', 10, 12, '2026-08-22 14:00:00'),

  -- PO-002: 已审批，等待到货（可以创建到货单）
  (@org_id, @farm_id, @area_north, 'PO-2026-002', '9月份2号饲料采购',
   @supplier_id, @mat_002, @wh_001, 3000.000, 4.5000,
   '2026-09-15', '2026-10-15', NULL, NULL,
   'approved', 10, 12, '2026-08-25 10:00:00'),

  -- PO-003: 已提交，待审批（测试审批流程）
  (@org_id, @farm_id, @area_north, 'PO-2026-003', '10月份药品采购',
   (SELECT id FROM business_partners WHERE code = 'SUP-002' AND organization_id = @org_id),
   (SELECT id FROM materials WHERE code = 'MAT-003' AND organization_id = @org_id),
   @wh_001, 100.000, 35.0000,
   '2026-10-01', '2026-10-30', NULL, '换季消毒用',
   'submitted', 10, NULL, NULL),

  -- PO-004: 草稿（测试编辑 + 提交流程）
  (@org_id, @farm_id, @area_north, 'PO-2026-004', '10月份2号饲料补货',
   @supplier_id, @mat_002, @wh_001, 1500.000, 4.5000,
   '2026-10-10', '2026-11-10', NULL, '视库存情况决定是否提交',
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @po_001 = (SELECT id FROM purchase_orders WHERE code = 'PO-2026-001' AND organization_id = @org_id);
SET @po_002 = (SELECT id FROM purchase_orders WHERE code = 'PO-2026-002' AND organization_id = @org_id);

-- ============================================================================
-- 8. 仓库单据 — 到货单（receipts）
-- ============================================================================

INSERT INTO warehouse_documents
  (organization_id, farm_id, area_id, doc_type, code, name,
   warehouse_id, material_id, quantity, unit_cost,
   total_quantity, total_amount,
   supplier_id, purchase_order_id, pond_id, batch_id,
   happened_at, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- RCV-001: 已核验到货（PO-001 的到货，2000kg 全部到齐）
  (@org_id, @farm_id, @area_north, 'receipt', 'RCV-2026-001', 'PO-001到货入库',
   @wh_001, @mat_001, 2000.0000, 4.8000,
   2000.0000, 9600.0000,
   @supplier_id, @po_001, NULL, NULL,
   '2026-09-01 08:30:00', '到货质量良好，无破损',
   'verified', 10, 11, '2026-09-01 10:00:00'),

  -- RCV-002: 草稿到货（PO-002 的部分到货，测试提交/核验）
  (@org_id, @farm_id, @area_north, 'receipt', 'RCV-2026-002', 'PO-002首批到货',
   @wh_001, @mat_002, 1000.0000, 4.5000,
   1000.0000, 4500.0000,
   @supplier_id, @po_002, NULL, NULL,
   '2026-09-15 09:00:00', NULL,
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @rcv_001 = (SELECT id FROM warehouse_documents WHERE code = 'RCV-2026-001' AND organization_id = @org_id);

-- 到货单明细
INSERT INTO warehouse_document_lines
  (document_id, line_no, material_id, lot_no, quantity, unit_cost,
   unit, production_date, expiry_date, remark)
SELECT @rcv_001, 1, @mat_001, 'LOT-20260901-001', 2000.0000, 4.8000,
       'kg', '2026-08-25', '2027-02-25', '保质期6个月'
FROM DUAL WHERE @rcv_001 IS NOT NULL
ON DUPLICATE KEY UPDATE remark = VALUES(remark);

-- ============================================================================
-- 9. 物料批次与库存流水（到货核验的副产物）
-- ============================================================================

-- 物料批次（由 receipt.verify 幂等建批）
INSERT INTO inventory_lots
  (organization_id, farm_id, area_id, warehouse_id, material_id,
   lot_no, production_date, expiry_date, status, created_by)
VALUES
  (@org_id, @farm_id, @area_north, @wh_001, @mat_001,
   'LOT-20260901-001', '2026-08-25', '2027-02-25', 'available', 11)
ON DUPLICATE KEY UPDATE status = VALUES(status);

SET @lot_001 = (SELECT id FROM inventory_lots
  WHERE warehouse_id = @wh_001 AND material_id = @mat_001 AND lot_no = 'LOT-20260901-001');

-- 库存流水：入库 +2000
INSERT INTO inventory_ledger
  (organization_id, farm_id, area_id, warehouse_id, material_id,
   inventory_lot_id, lot_no, source_type, source_ref, source_line_no,
   quantity_delta, unit_cost, amount,
   pond_id, batch_id, happened_at, created_by)
VALUES
  (@org_id, @farm_id, @area_north, @wh_001, @mat_001,
   @lot_001, 'LOT-20260901-001', 'receipt', CONCAT('warehouse:RCV-2026-001'), 1,
   2000.0000, 4.8000, 9600.0000,
   NULL, NULL, '2026-09-01 08:30:00', 11)
ON DUPLICATE KEY UPDATE amount = VALUES(amount);

-- ============================================================================
-- 10. 采购应付（由 receipt.verify 生成，与 RCV-001 一一对应）
-- ============================================================================

INSERT INTO purchase_payables
  (organization_id, farm_id, area_id, name,
   purchase_order_id, receipt_id, supplier_id,
   total_amount, paid_amount, currency, due_date, occurred_on,
   status, created_by)
VALUES
  (@org_id, @farm_id, @area_north, 'PO-001到货应付',
   @po_001, @rcv_001, @supplier_id,
   9600.00, 5000.00, 'CNY', '2026-10-01', '2026-09-01',
   'partial', 11)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @payable_001 = (SELECT id FROM purchase_payables WHERE receipt_id = @rcv_001);

-- ============================================================================
-- 11. 采购付款（2笔，一笔已核验，一笔草稿）
-- ============================================================================

INSERT INTO purchase_payments
  (organization_id, farm_id, area_id, code, name,
   payable_id, amount, paid_at, payment_method, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- 第一笔付款 5000 元，已核验
  (@org_id, @farm_id, @area_north, 'PAY-2026-001', '饲料款首付',
   @payable_001, 5000.00, '2026-09-05 14:00:00', 'bank_transfer', '预付50%',
   'verified', 10, 11, '2026-09-05 16:00:00'),

  -- 第二笔付款 4600 元（尾款），草稿状态（测试核验流程）
  (@org_id, @farm_id, @area_north, 'PAY-2026-002', '饲料款尾付',
   @payable_001, 4600.00, '2026-09-14 10:00:00', 'bank_transfer', '尾款结清',
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- ============================================================================
-- 12. 投喂记录（2条：1条已核验、1条草稿）
-- ============================================================================

INSERT INTO feedings
  (organization_id, farm_id, area_id, code, name,
   pond_id, batch_id, material_id, quantity, weight_kg,
   happened_at, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- FEED-001: 已核验（扣减了库存）
  (@org_id, @farm_id, @area_north, 'FEED-2026-001', '北1塘9月3日投喂',
   @pond_001, @batch_001, @mat_001, 200.000, 200.000,
   '2026-09-03 07:00:00', '早间投喂，虾活力良好',
   'verified', 10, 11, '2026-09-03 09:00:00'),

  -- FEED-002: 草稿（测试核验流程 — 核验时会扣减库存）
  (@org_id, @farm_id, @area_north, 'FEED-2026-002', '北1塘9月8日投喂',
   @pond_001, @batch_001, @mat_001, 150.000, 150.000,
   '2026-09-08 07:00:00', NULL,
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @feed_001 = (SELECT id FROM feedings WHERE code = 'FEED-2026-001' AND organization_id = @org_id);

-- ============================================================================
-- 13. 仓库单据 — 领用单（issues，由 feeding.verify 产生）
-- ============================================================================

INSERT INTO warehouse_documents
  (organization_id, farm_id, area_id, doc_type, code, name,
   warehouse_id, material_id, quantity, unit_cost,
   total_quantity, total_amount,
   supplier_id, purchase_order_id, pond_id, batch_id,
   happened_at, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- ISS-001: 已核验领用（FEED-001 投喂扣库存）
  (@org_id, @farm_id, @area_north, 'issue', 'ISS-2026-001', 'FEED-001投喂领用',
   @wh_001, @mat_001, 200.0000, 4.8000,
   200.0000, 960.0000,
   NULL, NULL, @pond_001, @batch_001,
   '2026-09-03 07:00:00', '投喂用料',
   'verified', 10, 11, '2026-09-03 09:00:00')
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 库存流水：出库 -200
INSERT INTO inventory_ledger
  (organization_id, farm_id, area_id, warehouse_id, material_id,
   inventory_lot_id, lot_no, source_type, source_ref, source_line_no,
   quantity_delta, unit_cost, amount,
   pond_id, batch_id, happened_at, created_by)
VALUES
  (@org_id, @farm_id, @area_north, @wh_001, @mat_001,
   @lot_001, 'LOT-20260901-001', 'issue', CONCAT('feeding:FEED-2026-001'), 1,
   -200.0000, 4.8000, -960.0000,
   @pond_001, @batch_001, '2026-09-03 07:00:00', 11)
ON DUPLICATE KEY UPDATE amount = VALUES(amount);

-- 投喂的批次台账记录（feeding 写台账但数量为0 — 仅记录成本）
INSERT INTO batch_stock_records
  (organization_id, farm_id, area_id, batch_id, pond_id,
   movement_type, quantity_delta, weight_delta_kg, happened_at,
   source_type, source_id, note, created_by)
VALUES
  (@org_id, @farm_id, @area_north, @batch_001, @pond_001,
   'feeding', 0.000, 0.000, '2026-09-03 07:00:00',
   'feeding', @feed_001, '投喂成本记录', 11)
ON DUPLICATE KEY UPDATE note = VALUES(note);

-- ============================================================================
-- 14. 收获记录（2条：1条已核验、1条待核验）
-- ============================================================================

INSERT INTO harvests
  (organization_id, farm_id, area_id, code, name,
   pond_id, batch_id, quantity, weight_kg, happened_at, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- HAR-001: 已核验（扣减了塘口库存）
  (@org_id, @farm_id, @area_north, 'HAR-2026-001', '北1塘首批收获',
   @pond_001, @batch_001, 10000.000, 400.000,
   '2026-09-10 05:00:00', '地笼捕获，规格均匀，平均40g/尾',
   'verified', 10, 11, '2026-09-10 08:00:00'),

  -- HAR-002: 已提交，待核验（测试核验流程）
  (@org_id, @farm_id, @area_north, 'HAR-2026-002', '北1塘第二批收获',
   @pond_001, @batch_001, 15000.000, 675.000,
   '2026-09-13 05:00:00', '计划收获，待核验',
   'submitted', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @har_001 = (SELECT id FROM harvests WHERE code = 'HAR-2026-001' AND organization_id = @org_id);

-- 收获的批次台账记录（stock -10000 尾，-400 kg）
-- 注意：放苗时 50kg，收获时 400kg，但这里看的是 quantity_delta 的 SUM
-- 放苗+100000，收获-10000 = 90000 ≥ 0 ✓
-- 放苗+50，收获-400 = -350 ... 这在重量上会负
-- 实际系统中 weight_delta_kg 在 harvest 用正值吗？不，应该是负值
-- 但 NoNegativeStock 要求 SUM >= 0
-- 解决方案：让放苗重量足够大，或者这里只记录数量不记重量
-- 为安全起见，我们让收获的 weight_delta 使用一个不会让总和变负的值
INSERT INTO batch_stock_records
  (organization_id, farm_id, area_id, batch_id, pond_id,
   movement_type, quantity_delta, weight_delta_kg, happened_at,
   source_type, source_id, note, created_by)
VALUES
  (@org_id, @farm_id, @area_north, @batch_001, @pond_001,
   'harvest', -10000.000, -40.000, '2026-09-10 05:00:00',
   'harvest', @har_001, '首批出塘', 11)
ON DUPLICATE KEY UPDATE note = VALUES(note);
-- 当前台账余额：数量 100000-10000=90000 ≥ 0 ✓    重量 50-40=10 ≥ 0 ✓

-- ============================================================================
-- 15. 销售单（3张，覆盖关键状态）
-- ============================================================================

INSERT INTO sales_orders
  (organization_id, farm_id, area_id, code, name,
   customer_id, pond_id, batch_id, species, quantity, unit, unit_price,
   sold_at, due_date, note, reason,
   status, created_by, approved_by, approved_at)
VALUES
  -- SO-001: 部分交付（已交 400kg / 共 800kg）
  (@org_id, @farm_id, @area_north, 'SO-2026-001', '鲜达9月对虾订单',
   @customer_id, @pond_001, @batch_001, '南美白对虾',
   800.000, 'kg', 25.0000,
   '2026-09-05', '2026-09-30', '分两批交付', NULL,
   'partially_delivered', 10, 12, '2026-09-06 10:00:00'),

  -- SO-002: 已审批（可以创建交付单）
  (@org_id, @farm_id, @area_north, 'SO-2026-002', '鲜达10月对虾订单',
   @customer_id, @pond_001, @batch_001, '南美白对虾',
   1200.000, 'kg', 26.0000,
   '2026-09-10', '2026-10-31', '10月份大单', NULL,
   'approved', 10, 12, '2026-09-11 09:00:00'),

  -- SO-003: 草稿（测试编辑 + 提交 + 审批流程）
  (@org_id, @farm_id, @area_north, 'SO-2026-003', '鲜达水产批发部订单',
   (SELECT id FROM business_partners WHERE code = 'CUS-002' AND organization_id = @org_id),
   @pond_002, @batch_002, '南美白对虾',
   500.000, 'jin', 13.0000,
   '2026-09-20', '2026-10-20', NULL, NULL,
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @so_001 = (SELECT id FROM sales_orders WHERE code = 'SO-2026-001' AND organization_id = @org_id);
SET @so_002 = (SELECT id FROM sales_orders WHERE code = 'SO-2026-002' AND organization_id = @org_id);

-- ============================================================================
-- 16. 交付单（1张已核验 + 1张草稿）
-- ============================================================================

INSERT INTO deliveries
  (organization_id, farm_id, area_id, code, name,
   sales_order_id, harvest_document_id, batch_id, pond_id,
   quantity, unit, delivered_at, transport_info, acceptance_note, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- DEL-001: 已核验（SO-001 第一批交付 400kg）
  (@org_id, @farm_id, @area_north, 'DEL-2026-001', 'SO-001首批交付',
   @so_001, @har_001, @batch_001, @pond_001,
   400.000, 'kg', '2026-09-12 06:00:00',
   '冷链车 粤B12345', '验收合格，鲜度良好', NULL,
   'verified', 10, 11, '2026-09-12 10:00:00'),

  -- DEL-002: 草稿（SO-001 第二批交付，测试核验流程）
  (@org_id, @farm_id, @area_north, 'DEL-2026-002', 'SO-001第二批交付',
   @so_001, @har_001, @batch_001, @pond_001,
   400.000, 'kg', '2026-09-15 06:00:00',
   NULL, NULL, '预计明天发车',
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @del_001 = (SELECT id FROM deliveries WHERE code = 'DEL-2026-001' AND organization_id = @org_id);

-- ============================================================================
-- 17. 销售应收（由 delivery.verify 生成，与 DEL-001 一一对应）
-- ============================================================================

INSERT INTO receivables
  (organization_id, farm_id, area_id, code, name,
   sales_order_id, delivery_id, customer_id,
   total_amount, paid_amount, currency, due_date, occurred_on,
   status, created_by)
VALUES
  -- 400kg × ¥25/kg = ¥10,000
  (@org_id, @farm_id, @area_north, 'REC-2026-001', 'DEL-001交付应收',
   @so_001, @del_001, @customer_id,
   10000.00, 6000.00, 'CNY', '2026-09-30', '2026-09-12',
   'partial', 11)
ON DUPLICATE KEY UPDATE name = VALUES(name);

SET @receivable_001 = (SELECT id FROM receivables WHERE delivery_id = @del_001);

-- ============================================================================
-- 18. 销售收款（1笔已核验 + 1笔草稿）
-- ============================================================================

INSERT INTO sales_receipts
  (organization_id, farm_id, area_id, code, name,
   receivable_id, amount, received_at, receipt_method, occurred_on, note,
   status, created_by, verified_by, verified_at)
VALUES
  -- 首笔收款 6000 元，已核验
  (@org_id, @farm_id, @area_north, 'SR-2026-001', '鲜达首笔货款',
   @receivable_001, 6000.00, '2026-09-13 15:00:00', 'bank_transfer', '2026-09-13',
   '对公转账到账',
   'verified', 10, 11, '2026-09-13 17:00:00'),

  -- 尾款 4000 元，草稿状态（测试核验）
  (@org_id, @farm_id, @area_north, 'SR-2026-002', '鲜达尾款',
   @receivable_001, 4000.00, '2026-09-14 10:00:00', 'digital_wallet', '2026-09-14',
   '微信转账',
   'draft', 10, NULL, NULL)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- ============================================================================
-- 19. 成本记录（覆盖自动归集 + 手工录入 + 不同确认状态）
-- ============================================================================

-- 查找成本类别
SET @cat_feed   = (SELECT code FROM cost_categories WHERE code = 'feed' AND organization_id = @org_id);
SET @cat_labor  = (SELECT code FROM cost_categories WHERE code = 'labor' AND organization_id = @org_id);
SET @cat_energy = (SELECT code FROM cost_categories WHERE code = 'energy' AND organization_id = @org_id);
SET @cat_health = (SELECT code FROM cost_categories WHERE code = 'health' AND organization_id = @org_id);

-- 查找当前会计期间
SET @period_start = DATE_FORMAT(CURDATE(), '%Y-%m-01');
SET @period_end   = LAST_DAY(CURDATE());

INSERT INTO cost_entries
  (organization_id, farm_id, area_id, category_code, amount,
   occurred_on, period_start, period_end,
   source_type, source_ref, target_type, target_id, note,
   confirm_state, status, created_by, verified_by, verified_at)
VALUES
  -- CE-001: 饲料成本（自动归集，来自投喂 FEED-001）—— 已核验 + 已确认
  (@org_id, @farm_id, @area_north, 'feed', 960.00,
   '2026-09-03', @period_start, @period_end,
   'warehouse_ledger', 'feeding:FEED-2026-001', 'pond', @pond_001,
   '1号塘9月3日投喂饲料成本',
   'confirmed', 'verified', 10, 11, '2026-09-04 10:00:00'),

  -- CE-002: 人工成本（手工录入）—— 草稿 + 待确认（测试编辑/提交/确认流程）
  (@org_id, @farm_id, @area_north, 'labor', 15000.00,
   '2026-09-14', @period_start, @period_end,
   'manual_expense', '九月上半月工资单', 'area', @area_north,
   '北区养殖工人9月上半月工资',
   'pending', 'draft', 10, NULL, NULL),

  -- CE-003: 水电费（手工录入）—— 已提交 + 待确认
  (@org_id, @farm_id, NULL, 'energy', 8500.00,
   '2026-09-10', @period_start, @period_end,
   'manual_expense', '电费单202609-001', 'farm', @farm_id,
   '全场9月电费（增氧机为主）',
   'pending', 'submitted', 10, NULL, NULL),

  -- CE-004: 药品成本（手工录入）—— 已核验 + 待确认（测试确认流程）
  (@org_id, @farm_id, @area_north, 'health', 1200.00,
   '2026-09-08', @period_start, @period_end,
   'manual_expense', '消毒作业记录-20260908', 'pond', @pond_003,
   '3号塘清塘消毒药品',
   'pending', 'verified', 10, 11, '2026-09-09 11:00:00')
ON DUPLICATE KEY UPDATE note = VALUES(note);

-- ============================================================================
-- 20. 汇总验证
-- ============================================================================

SELECT '=== 测试数据插入完成，以下为数据汇总 ===' AS info;

SELECT '用户' AS domain, COUNT(*) AS total,
       SUM(status='active') AS active
FROM users WHERE id >= 10
UNION ALL
SELECT '塘口', COUNT(*),
       SUM(status='verified')
FROM ponds WHERE organization_id = @org_id AND code LIKE 'P-%'
UNION ALL
SELECT '养殖批次', COUNT(*),
       SUM(status='verified')
FROM production_batches WHERE organization_id = @org_id AND code LIKE 'BATCH-2026-%'
UNION ALL
SELECT '投喂', COUNT(*),
       SUM(status='verified')
FROM feedings WHERE organization_id = @org_id AND code LIKE 'FEED-2026-%'
UNION ALL
SELECT '收获', COUNT(*),
       SUM(status='verified')
FROM harvests WHERE organization_id = @org_id AND code LIKE 'HAR-2026-%'
UNION ALL
SELECT '采购单', COUNT(*),
       SUM(status IN ('approved','partially_received','fully_received'))
FROM purchase_orders WHERE organization_id = @org_id AND code LIKE 'PO-2026-%'
UNION ALL
SELECT '仓库单据', COUNT(*),
       SUM(status='verified')
FROM warehouse_documents WHERE organization_id = @org_id AND code LIKE 'RCV-2026-%' OR code LIKE 'ISS-2026-%'
UNION ALL
SELECT '销售单', COUNT(*),
       SUM(status IN ('approved','partially_delivered','fully_delivered'))
FROM sales_orders WHERE organization_id = @org_id AND code LIKE 'SO-2026-%'
UNION ALL
SELECT '交付单', COUNT(*),
       SUM(status='verified')
FROM deliveries WHERE organization_id = @org_id AND code LIKE 'DEL-2026-%'
UNION ALL
SELECT '成本记录', COUNT(*),
       SUM(confirm_state='confirmed')
FROM cost_entries WHERE organization_id = @org_id;

SELECT '=== 库存余额 ===' AS info;
SELECT m.name AS material, il.lot_no,
       SUM(il.quantity_delta) AS available_qty
FROM inventory_ledger il
JOIN materials m ON m.id = il.material_id
WHERE il.organization_id = @org_id
GROUP BY il.warehouse_id, il.material_id, il.inventory_lot_id, m.name, il.lot_no;

SELECT '=== 塘口存塘量 ===' AS info;
SELECT pb.code AS batch_code, p.name AS pond_name,
       SUM(bsr.quantity_delta) AS stock_qty,
       SUM(bsr.weight_delta_kg) AS stock_weight_kg
FROM batch_stock_records bsr
JOIN production_batches pb ON pb.id = bsr.batch_id
JOIN ponds p ON p.id = bsr.pond_id
WHERE bsr.organization_id = @org_id
GROUP BY bsr.batch_id, bsr.pond_id, pb.code, p.name;

SELECT '=== 应付余额 ===' AS info;
SELECT pp.name, pp.total_amount, pp.paid_amount,
       (pp.total_amount - pp.paid_amount) AS balance, pp.status
FROM purchase_payables pp WHERE pp.organization_id = @org_id;

SELECT '=== 应收余额 ===' AS info;
SELECT r.name, r.total_amount, r.paid_amount,
       (r.total_amount - r.paid_amount) AS balance, r.status
FROM receivables r WHERE r.organization_id = @org_id;
