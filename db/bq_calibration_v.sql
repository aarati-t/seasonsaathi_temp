-- F7 calibration view for BigQuery (build block 6). Same logic as db/schema.sql calibration_v;
-- only the table references are dataset-qualified. Create with:
--   bq query --use_legacy_sql=false < db/bq_calibration_v.sql   (after replacing ${DATASET})
CREATE OR REPLACE VIEW `${DATASET}.calibration_v` AS
WITH base AS (
    SELECT o.predicted_signal, o.predicted_confidence, o.actual_result, o.is_synthetic,
           COALESCE(o.provenance, CASE WHEN o.is_synthetic THEN 'synthetic' WHEN o.season LIKE 'learning-demo%' THEN 'demo' ELSE 'legacy_unverified' END) AS provenance,
           COALESCE(o.rain_deficit_bucket, sr.rain_deficit_bucket, 'unknown') AS bucket,
           COALESCE(h.irrigation, 'unknown') AS irrigation,
           COALESCE(o.crop_key, ck.crop_key, 'unknown') AS crop
    FROM `${DATASET}.outcomes` o
    JOIN `${DATASET}.households` h ON h.hh_id = o.hh_id
    LEFT JOIN `${DATASET}.season_rain` sr ON sr.village_id = h.village_id AND sr.season = o.season
    LEFT JOIN `${DATASET}.crop_mix_keys` ck ON ck.decision = o.decision
    WHERE o.predicted_signal IS NOT NULL AND o.predicted_confidence IS NOT NULL AND o.actual_result IS NOT NULL
)
SELECT CONCAT(bucket, '|', irrigation, '|', crop) AS context_bucket,
       predicted_confidence AS confidence_level,
       AVG(CASE WHEN (predicted_signal = 'ACT' AND actual_result = 'good')
                  OR (predicted_signal = 'AVOID' AND actual_result <> 'good')
                  OR (predicted_signal = 'WAIT' AND actual_result = 'partial') THEN 1 ELSE 0 END) AS hit_rate,
       COUNT(*) AS n,
       SUM(CASE WHEN provenance = 'observed' THEN 1 ELSE 0 END) AS n_real,          -- observed only (P5b D)
       SUM(CASE WHEN provenance = 'synthetic' THEN 1 ELSE 0 END) AS n_synthetic,
       SUM(CASE WHEN provenance = 'demo' THEN 1 ELSE 0 END) AS n_demo,
       SUM(CASE WHEN provenance = 'legacy_unverified' THEN 1 ELSE 0 END) AS n_unverified
FROM base
GROUP BY 1, 2;
