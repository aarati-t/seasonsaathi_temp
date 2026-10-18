"""Data access interface. ALL DB access goes through here (DATA_BACKEND=postgres|bigquery).

Table and column names match db/schema.sql (SPEC section 5) on every backend.
"""
import os
from abc import ABC, abstractmethod

from dotenv import load_dotenv

load_dotenv()

# Columns per table, and the key used for idempotent upserts.
TABLES: dict[str, dict[str, list[str]]] = {
    "villages": {
        "key": ["village_id"],
        "cols": ["village_id", "name", "taluka", "district", "lat", "lon"],
    },
    "households": {
        "key": ["hh_id"],
        "cols": [
            "hh_id", "village_id", "tenure", "acres", "irrigation", "water_hours_per_week",
            "crops_last_season", "cattle_count", "goats_count", "fodder_weeks_left",
            "off_farm_income", "loan_due_month", "cash_buffer_band", "primary_cultivator_gender",
            "consent", "profile_text_norm", "profile_embedding", "embed_model", "is_synthetic", "created_at",
            "soil_type_farmer",
        ],
    },
    "plots": {"key": ["plot_id"], "cols": ["plot_id", "hh_id", "lat", "lon", "is_synthetic"]},
    "rain_series": {
        "key": ["village_id", "date"],
        "cols": ["village_id", "date", "rain_mm", "normal_mm", "cum_deficit_pct", "source"],
    },
    "ndvi_series": {"key": ["plot_id", "date"], "cols": ["plot_id", "date", "ndvi", "ndvi_normal", "source"]},
    "soil_moisture": {"key": ["village_id", "date"], "cols": ["village_id", "date", "sm_surface", "sm_rootzone"]},
    "soil_props": {"key": ["plot_id"], "cols": ["plot_id", "texture_class", "clay_pct", "sand_pct", "organic_carbon_pct",
                                                "ph", "water_fc_pct", "depth_cm", "source", "source_url", "is_synthetic"]},
    "crop_water_need": {"key": ["crop_key", "season"], "cols": [
        "crop_key", "crop", "season", "need_mm_low", "need_mm_high", "root_depth_m_low", "root_depth_m_high", "depletion_p",
        "critical_stage", "region", "basis", "is_synthetic", "note", "source_url", "root_source_url"]},
    "prices": {
        "key": ["commodity", "district", "market", "date", "source"],
        "cols": [
            "commodity", "district", "market", "date", "modal_price", "reference_price", "reference_type",
            "reference_season", "pct_vs_reference", "source", "source_url", "is_synthetic",
        ],
    },
    "price_summary": {
        "key": ["commodity", "district"],
        "cols": [
            "commodity", "district", "latest_date", "latest_modal_price", "reference_price", "reference_type",
            "reference_season", "reference_source_url", "pct_vs_reference", "n_markets", "note", "source",
            "source_url", "is_synthetic",
        ],
    },
    "livestock_prices": {
        "key": ["item_key"],
        "cols": [
            "item_key", "item", "unit", "region", "period", "value_low", "value_high", "value_mid",
            "basis", "is_synthetic", "note", "source_url",
        ],
    },
    "outcomes": {
        "key": ["outcome_id"],
        "cols": [
            "outcome_id", "hh_id", "season", "decision", "predicted_signal", "predicted_confidence",
            "actual_result", "cattle_sold", "distress_sale", "request_id", "rain_deficit_bucket", "crop_key", "provenance",
            "note", "is_synthetic",
        ],
    },
    "season_rain": {"key": ["village_id", "season"],
                    "cols": ["village_id", "season", "rain_deficit_bucket", "source", "is_synthetic"]},
    "crop_mix_keys": {"key": ["decision"], "cols": ["decision", "crop_key"]},
    "calibration_v": {  # read-only view
        "key": ["context_bucket", "confidence_level"],
        "cols": ["context_bucket", "confidence_level", "hit_rate", "n", "n_real", "n_synthetic"],
    },
    "feedback": {"key": ["feedback_id"],
                 "cols": ["feedback_id", "request_id", "feature", "context_bucket", "rating", "note", "created_at"]},
    "term_review": {"key": ["review_id"],
                    "cols": ["review_id", "term", "meaning", "category", "canonical", "status", "request_id",
                             "created_at"]},
    "alerts": {
        "key": ["alert_id"],
        "cols": ["alert_id", "plot_id", "hh_id", "stress_level", "next_step", "text_mr", "text_hi", "fact_ids",
                 "data_as_of", "request_id", "created_at", "via", "message_id"],
    },
    "impact_cache": {"key": ["cache_key"], "cols": [
        "cache_key", "hh_id", "feature", "engine", "model", "snapshot_hash", "version_hash", "result_json",
        "model_calls", "latency_ms", "created_at"]},
    "impact_runs": {"key": ["run_id"], "cols": [
        "run_id", "created_at", "population_snapshot_id", "n_households", "kpis_json", "assumptions_json",
        "engine_requested", "engines_used", "data_backend", "cache_snapshot_id", "benchmark_run_id", "version_metadata"]},
    "verifier_benchmark": {"key": ["benchmark_run_id", "case_id"], "cols": [
        "benchmark_run_id", "case_id", "created_at", "case_type", "expected", "outcome", "detail_json", "latency_ms",
        "model_calls", "version_metadata"]},
    "decision_log": {
        "key": ["request_id"],
        "cols": [
            "request_id", "hh_id", "feature", "facts_used", "output_json", "verifier_status",
            "orchestrator_used", "fallback_reason", "latency_ms", "genai_backend", "gemini_model",
        ],
    },
}


def check_columns(table: str, cols) -> None:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    unknown = set(cols) - set(TABLES[table]["cols"])
    if unknown:
        raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")


class Repo(ABC):
    @abstractmethod
    def init_schema(self) -> None:
        """Create tables if they do not exist."""

    @abstractmethod
    def upsert(self, table: str, rows: list[dict]) -> int:
        """Insert or update rows by the table key. Only the given columns are written."""

    @abstractmethod
    def insert(self, table: str, rows: list[dict]) -> int:
        """Plain insert; fails if a key already exists. One statement (works on BigQuery too)."""

    def replace_table(self, table: str, rows: list[dict]) -> int:
        """Reference data reload. Postgres: idempotent upsert. BigQuery overrides with WRITE_TRUNCATE."""
        return self.upsert(table, rows)

    def table_exists(self, table: str) -> bool:
        return True   # Postgres: init_schema creates every table

    @abstractmethod
    def count(self, table: str) -> int: ...

    @abstractmethod
    def fetch_all(self, table: str) -> list[dict]: ...

    @abstractmethod
    def fetch_where(self, table: str, **equals) -> list[dict]:
        """Rows where every given column equals the value (text compared case-insensitively)."""

    @abstractmethod
    def set_embedding(self, hh_id: str, embedding: list[float], embed_model: str | None = None) -> None: ...

    @abstractmethod
    def truncate_all(self) -> None:
        """Delete all rows. Tests only."""


_SHARED: dict[str, Repo] = {}


def _shared_repo(backend: str) -> Repo:
    """One BigQuery client / one in-memory CSV store per process."""
    if backend not in _SHARED:
        if backend == "bigquery":
            from repo.bigquery import BigQueryRepo
            _SHARED[backend] = BigQueryRepo()
        else:
            from repo.csvrepo import CsvRepo
            _SHARED[backend] = CsvRepo()
    return _SHARED[backend]


def get_repo(url: str | None = None) -> Repo:
    backend = os.getenv("DATA_BACKEND", "postgres")
    if backend == "postgres":
        from repo.postgres import PostgresRepo

        return PostgresRepo(url or os.getenv("DATABASE_URL", "postgresql:///seasonsaathi"))
    if backend in ("bigquery", "csv"):
        return _shared_repo(backend)
    raise ValueError(f"DATA_BACKEND must be postgres|bigquery|csv, got {backend!r}")
