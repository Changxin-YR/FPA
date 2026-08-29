-- ============================================================================
-- 009 production: feeding warehouse/lot hints (2 nullable columns)
--
-- WHY THIS MIGRATION EXISTS
--
-- The 负责人 ruled that `feeding.create` gains two OPTIONAL fields
-- `warehouse_id` / `lot_no`. Before this, "which warehouse, which material lot
-- does this feeding draw from" was decided SERVER-SIDE (default warehouse + FEFO),
-- so the user could neither see the choice in advance nor override it.
--
-- The ruling's reasoning is worth preserving because it is general:
--   The registry Sec 2.6 field table is a DERIVED FACT (it inherits the old
--   frontend's field names, per Sec 2.1), NOT a normative decision that "users
--   must not choose a lot". Treating an inherited status quo as a design decision
--   is what let a business-consequential choice hide behind a default. The
--   requirement is not merely "visible" but "explainable AND overridable".
--
-- WHY THE HINT IS STORED instead of re-resolved at verify time:
--   `feeding.create` and `feeding.verify` are separate transactions, possibly
--   minutes apart and possibly by different people. If verify re-derived the lot
--   with FEFO, then the choice rendered to the user at create time would become a
--   LIE whenever FEFO picked something else in between. Storing the hint keeps
--   create and verify agreeing on WHICH lot the document is about.
--
-- CROSS-DOMAIN RULE COMPLIANCE (ROLLOUT_CONTRACT Sec 1.1):
--   `warehouse_id` references the warehouse domain's `warehouses` table, so it is
--   stored as BIGINT ONLY with NO foreign key. Cross-domain FKs are forbidden and
--   the constraint is carried by invariants instead -- the same treatment
--   `feedings.batch_id` already gets.
--
-- ADDITIVE AND SAFE: both columns are nullable, so existing rows stay valid and
-- the previous behaviour (unset -> default warehouse / FEFO) is unchanged.
--
-- Idempotent: MySQL has no `ADD COLUMN IF NOT EXISTS` (that is MariaDB), so this
-- reuses the stored-procedure pattern already established in
-- `002_session_and_csrf.sql`. The procedure is dropped immediately after use.
-- ============================================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS fpa_add_column_if_missing;

DELIMITER $$
CREATE PROCEDURE fpa_add_column_if_missing(
  IN p_table VARCHAR(64),
  IN p_column VARCHAR(64),
  IN p_definition VARCHAR(255)
)
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = p_table
      AND COLUMN_NAME = p_column
  ) THEN
    SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `', p_column, '` ', p_definition);
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END IF;
END$$
DELIMITER ;

CALL fpa_add_column_if_missing(
  'feedings', 'warehouse_id',
  'BIGINT UNSIGNED NULL COMMENT ''指定出库仓（可选）。为空则由 warehouse 域按默认仓 + FEFO 解析''');

CALL fpa_add_column_if_missing(
  'feedings', 'lot_no',
  'VARCHAR(64) NULL COMMENT ''指定物料批次号（可选）。为空则由 warehouse 域按 FEFO 解析；此处仅作提示，实际扣减仍由 warehouse 域判定''');

DROP PROCEDURE IF EXISTS fpa_add_column_if_missing;
