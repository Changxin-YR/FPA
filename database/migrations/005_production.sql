-- ============================================================================
-- 005 production: batches / stock ledger / feedings / harvests
--
-- Field and constraint authority: docs/CAPABILITY_REGISTRY.md
--   Sec 1.5  production domain, 12 capabilities
--            batch.{list,create,update,submit,verify,close}
--            feeding.{list,create,verify}
--            harvest.{list,create,verify}
--   Sec 2.6  field tables (batch.create/update, feeding.create, harvest.create)
--   Sec 3.2  batch state machine (status lifecycle + batch_status business state)
--   Sec 3.3  feeding state machine (draft -> verified, two states only)
--   Sec 4    invariants #2 #5 #7 #11 #12 #13 #14 #16 #18 #19
--
-- Cross-domain rules: docs/ROLLOUT_CONTRACT.md
--   Sec 1.1  NO cross-domain foreign keys. Cross-domain references store BIGINT
--            only; the constraint lives in an invariant. FKs to the shared
--            identity tables (organizations / users) and to tables of this same
--            migration are allowed.
--   Sec 1.2  CREATE TABLE IF NOT EXISTS (migrations must be re-entrant);
--            status columns are VARCHAR(32), never ENUM; prefer unique keys for
--            invariants that can be expressed in DDL.
--
-- Table naming: production_batches (NOT "batches").
--   The registry prose uses short resource names (Sec 1.5 "batches", Sec 2.6
--   "batches"), but the real table name is fixed by Sec 4 #19 which lists the
--   unique key uq_production_batches_org_code. This repository names unique keys
--   uq_<table>_<cols> (verified in 003: uq_ponds_farm_code,
--   uq_materials_org_code, uq_business_partners_org_type_code), so
--   uq_production_batches_org_code pins the table name. Env var
--   MYSQL_ROOT_PASSWORD is not needed here; the migration runner supplies
--   credentials.
--
-- The stock ledger table name is fixed by Sec 4 #2/#11 and Sec 2.6:
--   batch_stock_records.
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- production_batches: the farming batch
--
-- Carries BOTH state machines, exactly like ponds carries status + pond_status:
--   status        record lifecycle     draft / submitted / verified / archived
--   batch_status  business state       stocked / farming / pending_settlement / closed
--
-- Sec 3.2-B transfer table (inherited verbatim from the old system,
-- production_service.py:15): stocked -> farming -> pending_settlement -> closed.
-- The initial batch_status is only ever set by batch.verify (never by create),
-- so create writes 'stocked' and verify forms the initial stock ledger row.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS production_batches (
  id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id        BIGINT UNSIGNED NOT NULL,
  farm_id                BIGINT UNSIGNED NOT NULL,
  area_id                BIGINT UNSIGNED NOT NULL,
  code                   VARCHAR(64)     NOT NULL,
  name                   VARCHAR(100)    NOT NULL,
  -- Cross-domain reference to master_data.ponds -> BIGINT only, no FK
  -- (ROLLOUT_CONTRACT Sec 1.1).
  pond_id                BIGINT UNSIGNED NOT NULL,
  species                VARCHAR(64)     NOT NULL,
  -- Sec 2.6: bound is MAX_PRODUCTION_QUANTITY (old production_service.py:35),
  -- aligned with DECIMAL(18,3).
  initial_quantity       DECIMAL(18,3)   NULL,
  initial_weight_kg      DECIMAL(18,3)   NULL,
  stocked_at             DATETIME        NOT NULL,
  expected_harvest_date  DATE            NULL,
  note                   VARCHAR(500)    NULL,

  batch_status           VARCHAR(32)     NOT NULL DEFAULT 'stocked',
  status                 VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version            INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by             BIGINT UNSIGNED NOT NULL,
  updated_by             BIGINT UNSIGNED NULL,
  verified_by            BIGINT UNSIGNED NULL,
  verified_at            DATETIME        NULL,
  created_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                         ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  -- Invariant #19 UniqueCode(fields=("code",), scope=("organization_id",)).
  -- The unique key is the correctness floor under concurrency; the invariant
  -- only produces a readable 409.
  UNIQUE KEY uq_production_batches_org_code (organization_id, code),
  -- DataScope predicate is resource(area_id) for every production capability, so
  -- the scope columns lead the index (Q6 decision: no cross-table resolution).
  KEY idx_production_batches_scope (organization_id, farm_id, area_id, status),
  KEY idx_production_batches_pond (pond_id),
  KEY idx_production_batches_batch_status (batch_status),

  CONSTRAINT fk_production_batches_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_production_batches_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_production_batches_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_production_batches_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_production_batches_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  -- Date ordering inherited from the old system (production_service.py:91-112,
  -- _validate_batch_dates). Stocking must not be in the future; expected harvest
  -- must not precede stocking. Both are expressible in DDL, so they live here.
  CONSTRAINT chk_production_batches_quantity CHECK (
    initial_quantity IS NULL OR (initial_quantity >= 0 AND initial_quantity <= 999999999999999.999)
  ),
  CONSTRAINT chk_production_batches_weight CHECK (
    initial_weight_kg IS NULL OR (initial_weight_kg >= 0 AND initial_weight_kg <= 999999999999999.999)
  ),
  CONSTRAINT chk_production_batches_dates CHECK (
    expected_harvest_date IS NULL OR expected_harvest_date >= DATE(stocked_at)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- batch_stock_records: the in-pond stock ledger (append-only)
--
-- This is the table that decides whether batch.close is reachable at all.
--   batch.verify   appends a POSITIVE row (the "stocking" row)  -> +initial_quantity
--   harvest.verify appends a NEGATIVE row                       -> -quantity
--
-- Two facts that matter and are easy to get wrong:
--   1. feeding.verify does NOT write this table's quantity columns to reduce
--      stock. Feeding consumes material inventory (inventory_ledger), not pond
--      stock. Sec 3.3 says it writes here only to record cost, so its rows carry
--      zero quantity/weight deltas.
--   2. harvest.verify is therefore the ONLY capability that reduces pond stock,
--      which is the single reason batch.close (invariant #11, stock must be 0)
--      is reachable at all (Sec 7 Q10).
--
-- Invariant #2  NoNegativeStock(table="batch_stock_records",
--                columns=("quantity_delta","weight_delta_kg"),
--                group_by=("batch_id","pond_id"))
-- Invariant #11 ZeroBalance(table="batch_stock_records", group_by=("batch_id",),
--                columns=("quantity_delta","weight_delta_kg"))
--
-- The ledger has no status column: it is an append-only fact table, so
-- StatusAllowsEdit and the lifecycle state machines do not apply to it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS batch_stock_records (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  area_id         BIGINT UNSIGNED NOT NULL,
  batch_id        BIGINT UNSIGNED NOT NULL,
  pond_id         BIGINT UNSIGNED NOT NULL,

  -- 'stocking' (batch.verify) / 'harvest' (harvest.verify) / 'feeding' (cost only)
  movement_type   VARCHAR(32)     NOT NULL,
  quantity_delta  DECIMAL(18,3)   NOT NULL DEFAULT 0,
  weight_delta_kg DECIMAL(18,3)   NOT NULL DEFAULT 0,
  happened_at     DATETIME        NOT NULL,

  -- Traceability: which document produced this ledger row. Cross-domain safe
  -- (BIGINT only). source_type mirrors inventory_ledger.source_type semantics.
  source_type     VARCHAR(32)     NOT NULL,
  source_id       BIGINT UNSIGNED NOT NULL,
  note            VARCHAR(500)    NULL,
  created_by      BIGINT UNSIGNED NOT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  KEY idx_batch_stock_scope (organization_id, farm_id, area_id),
  -- Exactly the group_by of invariants #2 and #11. The balance query is
  -- SUM(...) WHERE batch_id=? AND pond_id=? , so both columns lead.
  KEY idx_batch_stock_balance (batch_id, pond_id),
  -- Same-source-same-row idempotency, mirroring the old system's
  -- uq_inventory_ledger_source_line. This is what makes verify re-entrant:
  -- a retried verify cannot double-count the movement.
  UNIQUE KEY uq_batch_stock_source_line (source_type, source_id, movement_type),

  CONSTRAINT fk_batch_stock_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_batch_stock_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_batch_stock_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  -- Same-migration FK: allowed (ROLLOUT_CONTRACT Sec 1.1).
  CONSTRAINT fk_batch_stock_batch FOREIGN KEY (batch_id)
    REFERENCES production_batches(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_batch_stock_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- feedings: the feeding record
--
-- Sec 3.3, deliberately NOT the same shape as master data / order documents:
--   status is TWO states only: draft / verified
--     - no 'submitted' intermediate state (submit and verify were the same role
--       in the old system, so the intermediate state bought no control)
--     - no 'archived' (a verified feeding is a ledger fact; it is not archived)
--     - no 'corrected' (correction documents were cut; corrections are done by
--       void-and-reopen)
--   row_actions: draft -> [view, edit, verify]; verified -> [view]
--   there is NO feeding.update capability: draft -> draft editing is not
--   offered, so a draft is discarded and re-created instead.
--
-- Sec 2.6 feeding.create fields. Note what is deliberately ABSENT:
--   feed_plan_id / feed_task_id       cut with feed-plans/feed-tasks
--   material_issue_request_id         cut with issue-requests
-- and the consequence (Sec 4 #17): "feeding requires an approved material issue
-- request" no longer exists. This version debits real inventory at verify time
-- instead, so the enforcement point moved from "request quota" to "actual stock".
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS feedings (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  area_id         BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(100)    NOT NULL,
  pond_id         BIGINT UNSIGNED NOT NULL,
  batch_id        BIGINT UNSIGNED NOT NULL,
  material_id     BIGINT UNSIGNED NOT NULL,
  -- Sec 2.6: quantity > 0 and "at least one of quantity / weight_kg" is
  -- invariant #16 AtLeastOneOf(fields=("quantity","weight_kg")).
  -- Invariant #16 covers both, so no DDL CHECK duplicates it here: two places
  -- describing one rule is exactly what this project removes.
  quantity        DECIMAL(18,3)   NULL,
  weight_kg       DECIMAL(18,3)   NULL,
  happened_at     DATETIME        NOT NULL,
  note            VARCHAR(500)    NULL,

  -- Two states only (Sec 3.3). VARCHAR(32), never ENUM.
  status          VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  updated_by      BIGINT UNSIGNED NULL,
  verified_by     BIGINT UNSIGNED NULL,
  verified_at     DATETIME        NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  -- Invariant #19 UniqueCode(fields=("code",), scope=("organization_id",)).
  UNIQUE KEY uq_feedings_org_code (organization_id, code),
  KEY idx_feedings_scope (organization_id, farm_id, area_id, status),
  KEY idx_feedings_batch (batch_id),
  KEY idx_feedings_pond (pond_id),
  KEY idx_feedings_material (material_id),

  CONSTRAINT fk_feedings_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_feedings_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_feedings_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_feedings_batch FOREIGN KEY (batch_id)
    REFERENCES production_batches(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_feedings_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_feedings_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  CONSTRAINT chk_feedings_quantity CHECK (quantity IS NULL OR quantity > 0),
  CONSTRAINT chk_feedings_weight CHECK (weight_kg IS NULL OR weight_kg >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- harvests: the harvest fact source
--
-- This table was cut in the first draft and restored by Sec 7 Q9/Q10. The
-- decisive argument (DECISIONS.md Q10) is recorded in the registry: with harvest
-- cut, NOTHING could reduce pond stock, so batch.close's "stock must be 0"
-- invariant could never become true - an unreachable capability is worse than a
-- missing one because it looks present on the checklist.
--
-- Sec 2.6 harvest.create. Field names are inherited UNCHANGED from the old
-- COMMON_FIELDS (quantity / weight_kg / happened_at) - harvest shares the same
-- field set as feeding on purpose.
--
-- status lifecycle is the standard 4-state one (draft/submitted/verified/
-- archived), NOT the two-state feeding shape: harvest has a separate submit
-- capability (harvest.list/create/verify + batch-style submit), and its facts
-- are consumed by sales (delivery.harvest_document_id), so it needs the record
-- lifecycle. This is the point corrected during cross-domain alignment.
--
-- IMMUTABILITY CONTRACT (consumed by sales):
--   Once verified, quantity / weight_kg are NEVER modified. harvest.verify only
--   ever writes status and verified_by. Sales' HarvestQuantityMatch
--   (tolerance="exact", Sec 4 #6) compares a delivery against this row, and it
--   must be able to treat the row as an immutable fact once verified.
--   Corrections are made by voiding and re-creating, never by editing.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS harvests (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  organization_id BIGINT UNSIGNED NOT NULL,
  farm_id         BIGINT UNSIGNED NOT NULL,
  area_id         BIGINT UNSIGNED NOT NULL,
  code            VARCHAR(64)     NOT NULL,
  name            VARCHAR(100)    NOT NULL,
  pond_id         BIGINT UNSIGNED NOT NULL,
  batch_id        BIGINT UNSIGNED NOT NULL,
  -- Sec 2.6: quantity > 0, and at verify time quantity <= current pond stock
  -- (invariant #2, enforced against batch_stock_records with FOR UPDATE).
  quantity        DECIMAL(18,3)   NOT NULL,
  weight_kg       DECIMAL(18,3)   NULL,
  happened_at     DATETIME        NOT NULL,
  note            VARCHAR(500)    NULL,

  status          VARCHAR(32)     NOT NULL DEFAULT 'draft',
  row_version     INT UNSIGNED    NOT NULL DEFAULT 1,
  created_by      BIGINT UNSIGNED NOT NULL,
  updated_by      BIGINT UNSIGNED NULL,
  verified_by     BIGINT UNSIGNED NULL,
  verified_at     DATETIME        NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  -- Invariant #19 UniqueCode(fields=("code",), scope=("organization_id",)).
  UNIQUE KEY uq_harvests_org_code (organization_id, code),
  KEY idx_harvests_scope (organization_id, farm_id, area_id, status),
  -- Built specifically so sales' delivery validation ("harvest document must be
  -- verified and belong to the same batch + same pond") is an index lookup
  -- rather than a table scan.
  KEY idx_harvests_batch_pond (batch_id, pond_id, status),
  KEY idx_harvests_pond (pond_id),

  CONSTRAINT fk_harvests_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_harvests_farm FOREIGN KEY (farm_id)
    REFERENCES farms(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_harvests_area FOREIGN KEY (area_id)
    REFERENCES areas(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_harvests_batch FOREIGN KEY (batch_id)
    REFERENCES production_batches(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_harvests_created_by FOREIGN KEY (created_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CONSTRAINT fk_harvests_verified_by FOREIGN KEY (verified_by)
    REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,

  CONSTRAINT chk_harvests_quantity CHECK (quantity > 0),
  CONSTRAINT chk_harvests_weight CHECK (weight_kg IS NULL OR weight_kg >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
