-- 保证部署后当前月份可用于登记、核验和关账。
-- 004 在初始化时按当时的日期预置期间；已有库升级时需要补当前月份这一行。
SET NAMES utf8mb4;

INSERT INTO accounting_periods
  (organization_id, period, period_start, period_end, status, created_by)
SELECT o.id,
       DATE_FORMAT(CURDATE(), '%Y-%m'),
       DATE_FORMAT(CURDATE(), '%Y-%m-01'),
       LAST_DAY(CURDATE()),
       'open',
       1
FROM organizations AS o
WHERE o.code = 'default'
  AND EXISTS (SELECT 1 FROM users WHERE id = 1)
ON DUPLICATE KEY UPDATE
  period_start = VALUES(period_start),
  period_end = VALUES(period_end);
