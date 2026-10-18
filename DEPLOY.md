# P6: GCP backends + deploy (BigQuery, Cloud Storage, Pub/Sub, Vertex AI, Cloud Run)

Infra already exists (created manually): service account `seasonsaathi-run@PROJECT`, dataset `seasonsaathi`
in `$REGION`, bucket `gs://PROJECT-seasonsaathi`, topic `stress-alerts`. Nothing below creates infra except
the Pub/Sub push subscription (step 5) and one IAM binding that is missing (step 0b).

Run all steps in one terminal from the repo root with the venv active.

## 0. Variables, and one missing permission
```bash
cd ~/Documents/Aarati/projects/seasonsaathi && source .venv/bin/activate
export PROJECT=migis-prototype REGION=asia-south1        # REGION = where the dataset was created
export SA=seasonsaathi-run@$PROJECT.iam.gserviceaccount.com GCS_BUCKET=$PROJECT-seasonsaathi
gcloud config set project $PROJECT
```
0b. The service account has no Cloud Storage role (needed for audio / evidence uploads):
```bash
gcloud storage buckets add-iam-policy-binding gs://$GCS_BUCKET --member serviceAccount:$SA --role roles/storage.objectAdmin
```

## 1. Load BigQuery (from your laptop, with your own gcloud login)
```bash
python scripts/gen_bq_schema.py                                   # db/bq_schema.json from db/schema.sql
GOOGLE_CLOUD_PROJECT=$PROJECT BQ_DATASET=seasonsaathi BQ_LOCATION=$REGION \
  python scripts/load_data.py --backend bigquery
```
Expect one line per table: `... rows (WRITE_TRUNCATE from CSV)`, `households 32 rows (WRITE_TRUNCATE from Postgres
(with embeddings))`, and `decision_log / feedback / term_review / alerts  untouched (app-written)`.
On later reloads, `outcomes` is SKIPPED if real outcomes exist, and `households` is SKIPPED if the app saved
households in BigQuery that are not in Postgres.

## 2. Compare row counts Postgres vs BigQuery
```bash
GOOGLE_CLOUD_PROJECT=$PROJECT BQ_DATASET=seasonsaathi BQ_LOCATION=$REGION python scripts/compare_counts.py
```
Expect `match yes` for every reference table and `RESULT: PASS` (app-written tables may differ).

## 3. Deploy
```bash
./deploy.sh
```
Builds from source (5–10 min first time) and prints `Service URL: https://seasonsaathi-....run.app`.
Vertex stays on `VERTEX_LOCATION=global` (gemini-3.5-flash is only served there for this project); Cloud Run and
BigQuery use `$REGION`. If Cloud Build reports a permission error on its build service account:
```bash
PN=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$PN-compute@developer.gserviceaccount.com \
  --role roles/run.builder --condition=None
```

## 4. Service URL
```bash
export URL=$(gcloud run services describe seasonsaathi --region $REGION --format 'value(status.url)')
curl -s $URL/api/whoami
```
Expect `"genai_backend":"vertex"`, `"location":"global"`, `"data_backend":"bigquery"`, `"storage_backend":"gcs"`,
`"alerts_mode":"pubsub"`. Startup log line: `gcloud run services logs read seasonsaathi --region $REGION | grep startup:`

## 5. Pub/Sub push subscription to the app
```bash
gcloud pubsub subscriptions create stress-alerts-push --topic stress-alerts \
  --push-endpoint "$URL/api/alerts/receive" --ack-deadline 30
```

## 6. Smoke test
```bash
scripts/smoke_test.sh $URL
```
Expect 7 lines `PASS ...` and `7 passed, 0 failed` (first F3/F4 calls take 15–30 s; F2 alerts arrive via Pub/Sub,
the script waits 5 s before GET /api/alerts).

## Emergency fallback (BigQuery blocks you > 30 min)
Read-only bundled data; app writes are kept in memory and lost on restart.
```bash
python scripts/export_bundle.py                 # households with embeddings -> data/bundle/households.csv
DATA_BACKEND_DEPLOY=csv ./deploy.sh
scripts/smoke_test.sh $URL                      # whoami shows "data_backend":"csv"
```

## Notes
- `--timeout 120`: `POST /api/f2/scan` over all plots takes longer than that; run the scan with
  `python scripts/run_f2.py --scan` (BigQuery env vars set) or deploy with `TIMEOUT=600 ./deploy.sh`.
- The URL is public (`--allow-unauthenticated`); the API has no login yet.
- Cold start: the app checks every BigQuery table and recreates the calibration view at startup (a few seconds).
  On demo day keep one instance warm without rebuilding: `gcloud run services update seasonsaathi --region $REGION --min-instances 1`
  (about Rs 10-15/hour while on; set back to `--min-instances 0` after). `MIN_INSTANCES=1 ./deploy.sh` does the same with a rebuild.
