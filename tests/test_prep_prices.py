"""prep_prices: name normalisation, pct maths, no invented prices, idempotent output; price tool facts."""
import pandas as pd
import pytest

import prep_prices as pp
from tools.prices import get_prices

AGMARKNET = (
    ",,,,Marketwise Price & Arrival Report (15-10-2026),,,,\r\n"
    ",,,Price (Rs./Quintal)  ,,,Arrival (Metric Tonnes)  ,,\r\n"
    'Commodity Group,Commodity,MSP (Rs./Quintal) 2026-27,"Price on 15 Oct, 2026","Price on 14 Oct, 2026",'
    '"Arrival on 15 Oct, 2026","Arrival on 14 Oct, 2026"\r\n'
    "{group},{name},1.00,{p1},{p2},1.0,1.0\r\n\r\n"
)
# Test-fixture reference prices, not real MSP values.
MSP = (
    "commodity,season,marketing_season,msp_rs_per_qtl,source_url\n"
    "soybean,kharif,KMS T,1000,https://example.test/kms\n"
    "safflower,rabi,RMS T,2000,https://example.test/rms\n"
)
CONFIG = (
    "soybean:\n  agmarknet_names: [\"Soyabean\"]\n  msp_key: soybean\n"
    "safflower:\n  agmarknet_names: [\"Safflower\"]\n  msp_key: safflower\n"
    "  missing_note: \"no mandi data (off-season); MSP only\"\n"
)


@pytest.fixture
def dirs(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir(), out.mkdir()
    (raw / "agmarknet_dharashiv_soybean.csv").write_text(
        AGMARKNET.format(group="Oil Seeds", name="Soyabean", p1="1250.00", p2="-"))
    (raw / "agmarknet_dharashiv_groundnut.csv").write_text(
        AGMARKNET.format(group="Oil Seeds", name="Groundnut", p1="900.00", p2="0"))
    (raw / "msp_2026.csv").write_text(MSP)
    (tmp_path / "commodities.yaml").write_text(CONFIG)
    return raw, out, tmp_path / "commodities.yaml"


def test_real_agmarknet_names_normalise():
    name_map = pp.build_name_map(pp.load_commodities())
    assert pp.normalise_commodity("Soyabean", name_map) == "soybean"
    assert pp.normalise_commodity("Red gram/Arhar/Tur(whole)", name_map) == "tur"
    assert pp.normalise_commodity("Arhar (Tur/Red Gram)(Whole)", name_map) == "tur"
    assert pp.normalise_commodity("Bengal Gram (Gram)(Whole)", name_map) == "gram"
    assert pp.normalise_commodity("jowar(sorghum)", name_map) == "jowar"
    assert pp.normalise_commodity("Groundnut", name_map) is None  # unknown: not guessed


def test_pct_vs_reference():
    assert pp.pct_vs_reference(7414.29, 5958) == 24.4
    assert pp.pct_vs_reference(900, 1000) == -10.0
    assert pp.pct_vs_reference(None, 1000) is None


def test_run_report_and_missing_commodity(dirs):
    raw, out, config = dirs
    report = pp.run(raw, out, config)
    assert report["found"] == ["soybean"]
    assert report["missing"] == ["safflower"]
    assert report["unknown"] == ["Groundnut"]
    assert report["kept"] == 1 and report["dropped"] == 3  # "-" + groundnut x2

    prices = pp.read_csv(out / "prices.csv")
    assert prices.commodity.tolist() == ["soybean"]
    assert prices.loc[0, "pct_vs_reference"] == 25.0
    assert not prices.is_synthetic.any()

    summary = pp.read_csv(out / "price_summary.csv").set_index("commodity")
    assert pd.isna(summary.loc["safflower", "latest_modal_price"])
    assert summary.loc["safflower", "reference_price"] == 2000
    assert summary.loc["safflower", "note"] == "no mandi data (off-season); MSP only"
    assert summary.loc["soybean", "latest_modal_price"] == 1250


def test_run_is_idempotent(dirs):
    raw, out, config = dirs
    pp.run(raw, out, config)
    first = [(out / f).read_bytes() for f in ("prices.csv", "price_summary.csv")]
    pp.run(raw, out, config)
    assert [(out / f).read_bytes() for f in ("prices.csv", "price_summary.csv")] == first


class FakeRepo:
    def __init__(self, rows):
        self.rows = rows

    def fetch_where(self, table, **eq):
        return [r for r in self.rows if all(str(r[k]).lower() == str(v).lower() for k, v in eq.items())]


def summary_row(**kw):
    row = dict(commodity="safflower", district="Dharashiv", latest_date=None, latest_modal_price=None,
               reference_price=7215.0, reference_type="MSP", reference_season="RMS 2027-28",
               reference_source_url="https://pib.example", pct_vs_reference=None, n_markets=None,
               note="no mandi data (off-season); MSP only", source="Agmarknet",
               source_url="https://agmarknet.gov.in/home", is_synthetic=False)
    row.update(kw)
    return row


def test_tool_missing_mandi_returns_only_msp_fact():
    facts = get_prices("safflower", "dharashiv", FakeRepo([summary_row()]))
    assert len(facts) == 1
    assert facts[0]["text"] == "MSP ₹7,215/qtl (RMS 2027-28); current mandi price unavailable"
    assert facts[0]["mandi_available"] is False


def test_tool_facts_carry_trace_fields():
    row = summary_row(commodity="gram", latest_date="2026-10-15", latest_modal_price=7414.29,
                      reference_price=5958.0, pct_vs_reference=24.4, note=None)
    facts = get_prices("gram", "Dharashiv", FakeRepo([row]))
    assert [f["fact_id"] for f in facts] == [
        "F-price-gram-dharashiv-mandi", "F-price-gram-dharashiv-msp", "F-price-gram-dharashiv-vs-msp"]
    for f in facts:
        assert {"fact_id", "value", "reference_type", "reference_season", "source", "source_url"} <= f.keys()
    assert facts[2]["value"] == 24.4


# ---------- livestock ----------
from tools.prices import get_fodder_intake, get_livestock_price_facts  # noqa: E402

LIVESTOCK_HEADER = "item_key,item,unit,region,period,value_low,value_high,value_mid,basis,is_synthetic,note,source_url\n"


def live_row(**kw):
    row = dict(item_key="kadba_kg_now", item="Jowar kadba", unit="Rs per kg", region="Dharashiv",
               period="Sep 2026", value_low=17.0, value_high=25.0, value_mid=20.0, basis="derived",
               is_synthetic=False, note=None, source_url="https://www.esakal.com/x")
    row.update(kw)
    return row


def test_livestock_fact_fields_and_range():
    f = get_livestock_price_facts(["kadba_kg_now"], FakeRepo([live_row()]))[0]
    assert f["fact_id"] == "F-live-kadba_kg_now"
    assert (f["value_mid"], f["range"], f["unit"], f["period"]) == (20.0, [17.0, 25.0], "Rs per kg", "Sep 2026")
    assert f["source"] == "esakal.com" and f["source_url"] == "https://www.esakal.com/x"
    assert f["is_synthetic"] is False


def test_assumption_rows_are_synthetic():
    row = live_row(item_key="desi_cow_normal_value", basis="assumption", is_synthetic=True, source_url=None)
    f = get_livestock_price_facts(["desi_cow_normal_value"], FakeRepo([row]))[0]
    assert f["is_synthetic"] is True and "SYNTHETIC" in f["text"]


def test_fodder_camp_rate_has_2019_caveat():
    row = live_row(item_key="fodder_camp_large_animal_day", unit="Rs per animal per day", period="2019 drought",
                   value_low=100.0, value_high=100.0, value_mid=100.0, basis="reported")
    f = get_livestock_price_facts(["fodder_camp_large_animal_day"], FakeRepo([row]))[0]
    assert f["caveat"] == "2019 rate; current rate not confirmed"
    assert "2019 rate; current rate not confirmed" in f["text"]


def test_fodder_intake_default_and_household_override():
    row = live_row(item_key="fodder_intake_large_animal_day", unit="kg per animal per day",
                   value_low=18.0, value_high=18.0, value_mid=18.0, basis="reported")
    repo = FakeRepo([row])
    assert get_fodder_intake({"hh_id": "SYN-001"}, repo)["value"] == 18.0
    own = get_fodder_intake({"hh_id": "SYN-001", "fodder_intake_kg_per_day": 12}, repo)
    assert own["value"] == 12 and own["source"] == "household profile"


def test_prep_rejects_unlabelled_assumption(dirs):
    raw, out, config = dirs
    (raw / "livestock_prices.csv").write_text(
        LIVESTOCK_HEADER + "cow_x,Cow,Rs per animal,D,Normal,1,2,1.5,assumption,false,,\n")
    with pytest.raises(SystemExit, match="is_synthetic=true"):
        pp.run(raw, out, config)


def test_prep_copies_livestock_values_unchanged(dirs):
    raw, out, config = dirs
    (raw / "livestock_prices.csv").write_text(
        LIVESTOCK_HEADER + "silage_kg,Silage,Rs per kg,D,Apr 2026,3,4,3.5,reported,false,n,https://a.test\n")
    report = pp.run(raw, out, config)
    got = pp.read_csv(out / "livestock_prices.csv").iloc[0]
    assert (got.value_low, got.value_high, got.value_mid) == (3, 4, 3.5)
    assert report["livestock"] == 1
