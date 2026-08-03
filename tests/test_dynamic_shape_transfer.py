"""Unit tests for Dynamics_predicter research pipeline."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from dynamics_predicter.transfer import (
    ANCHORS,
    DYN_ORDER,
    EXTRAPOLATED_BEYOND,
    INTERPOLATED,
    INTERVAL_KIND,
    bootstrap_diagnostics,
    calibrate_log_span_model,
    discover_panel_xlsx,
    export_excel,
    fit_log_span_model,
    holdout_all_baselines,
    holdout_anchor_diagnostics,
    is_dynamics_panel_filename,
    parse_paste_panel,
    place_equal_log_ladder,
    predict_held_anchor,
    pushforward_predictive_intervals,
    run_transfer,
    transfer_one_note,
    validate_panel_df,
    _detect_header_map,
)


def _mono_anchors(pp=10.0, mf=12.0, ff=16.0) -> dict[str, float]:
    return {"pp": pp, "mf": mf, "ff": ff}


def test_anchors_exact_never_overwritten():
    res = transfer_one_note(_mono_anchors(), use_pchip=False)
    assert res.status == "ok"
    for a in ANCHORS:
        assert res.target_pred[a] == pytest.approx(res.target_measured[a])
        assert res.pred_lo[a] == pytest.approx(res.target_measured[a])
        assert res.pred_hi[a] == pytest.approx(res.target_measured[a])


def test_anchors_exact_with_pchip():
    res = transfer_one_note(_mono_anchors(), use_pchip=True)
    assert res.status == "ok"
    for a in ANCHORS:
        assert res.target_pred[a] == pytest.approx(res.target_measured[a])


def test_equal_log_fractions_interior():
    tgt = _mono_anchors(10.0, 20.0, 40.0)
    res = transfer_one_note(tgt, use_pchip=False)
    log_p = math.log(10) + (1 / 3) * (math.log(20) - math.log(10))
    log_mp = math.log(10) + (2 / 3) * (math.log(20) - math.log(10))
    log_f = math.log(20) + 0.5 * (math.log(40) - math.log(20))
    assert res.target_pred["p"] == pytest.approx(math.exp(log_p), rel=1e-9)
    assert res.target_pred["mp"] == pytest.approx(math.exp(log_mp), rel=1e-9)
    assert res.target_pred["f"] == pytest.approx(math.exp(log_f), rel=1e-9)


def test_outer_soft_side_below_pp_when_rising():
    res = transfer_one_note(_mono_anchors(10.0, 20.0, 40.0), use_pchip=False)
    assert res.target_pred["ppp"] < res.target_pred["pp"]
    assert res.target_pred["pppp"] < res.target_pred["ppp"]
    assert res.target_pred["fff"] > res.target_pred["ff"]
    assert res.target_pred["ffff"] > res.target_pred["fff"]


def test_value_kinds():
    res = transfer_one_note(_mono_anchors(), use_pchip=False)
    for a in ANCHORS:
        assert res.value_kind[a] == "measured_anchor"
    for d in INTERPOLATED:
        assert res.value_kind[d] == "interpolated"
    for d in EXTRAPOLATED_BEYOND:
        assert res.value_kind[d] == "extrapolated_beyond"


def test_nonmonotonic_flag():
    res = transfer_one_note({"pp": 30.0, "mf": 20.0, "ff": 40.0}, use_pchip=False)
    assert "nonmonotonic_anchors" in res.flags
    assert res.quality < 0.9


def test_paste_parser_header_and_no_header():
    text = "note\tpp\tmf\tff\nG3\t10\t12\t16\n"
    df = parse_paste_panel(text)
    assert len(df) == 1
    assert df.iloc[0]["tgt_pp"] == 10.0
    df2 = parse_paste_panel("A4\t8.5\t9\t11\n")
    assert df2.iloc[0]["tgt_pp"] == pytest.approx(8.5)
    df3 = parse_paste_panel("B4\t8,5\t9\t11\n")
    assert df3.iloc[0]["tgt_pp"] == pytest.approx(8.5)


def test_paste_rejects_nonpositive():
    with pytest.raises(ValueError):
        parse_paste_panel("G3\t-1\t2\t3\n")


def test_validate_panel_duplicates():
    df = pd.DataFrame(
        [
            {"note": "G3", "tgt_pp": 1.0, "tgt_mf": 2.0, "tgt_ff": 3.0},
            {"note": "G3", "tgt_pp": 1.1, "tgt_mf": 2.1, "tgt_ff": 3.1},
        ]
    )
    errs = validate_panel_df(df)
    assert any("duplicate" in e for e in errs)


def test_holdout_production_aligned_uses_corpus_share():
    rows = []
    for i, base in enumerate(np.linspace(8, 20, 12)):
        pp = float(base)
        ff = pp * 1.5
        mf = pp * math.exp((2 / 3) * math.log(1.5))
        rows.append({"note": f"N{i}", "note_target": f"N{i}", "tgt_pp": pp, "tgt_mf": mf, "tgt_ff": ff})
    df = pd.DataFrame(rows)
    h = holdout_anchor_diagnostics(df, hold="mf", method="production_aligned")
    assert h["rel_err"]["mae"] < 0.02


def test_holdout_baselines_all_present():
    df = parse_paste_panel(
        "\n".join(
            [
                "note\tpp\tmf\tff",
                "G3\t10\t12\t16",
                "A3\t11\t13\t17",
                "B3\t9\t11\t15",
                "C4\t8\t10\t14",
                "D4\t12\t14\t18",
            ]
        )
    )
    hs = holdout_all_baselines(df)
    assert len(hs) == 9
    methods = {h["method"] for h in hs}
    assert methods == {"production_aligned", "log_midpoint", "linear"}


def test_predict_held_mf_log_midpoint():
    pred = predict_held_anchor({"pp": 4.0, "ff": 16.0}, hold="mf", method="log_midpoint")
    assert pred == pytest.approx(8.0)


def _toy_panel(n: int = 12) -> pd.DataFrame:
    rows = []
    for i in range(n):
        pp = 8.0 + i * 0.5
        mf = pp * 1.15
        ff = mf * 1.12
        rows.append(
            {
                "note": f"C{4 + (i % 3)}",
                "note_target": f"C{4 + (i % 3)}",
                "tgt_pp": pp,
                "tgt_mf": mf,
                "tgt_ff": ff,
                "complete_anchors": True,
            }
        )
    return pd.DataFrame(rows)


def test_intervals_model_pushforward_wider_on_imputed():
    df = _toy_panel()
    model = fit_log_span_model(df)
    assert model is not None
    res = transfer_one_note(
        _mono_anchors(10.0, 11.5, 12.88),
        use_pchip=False,
        span_model=model,
        note_target="C4",
        n_pushforward=200,
    )
    assert res.interval_kind == INTERVAL_KIND
    assert res.pred_lo["pp"] == pytest.approx(res.target_pred["pp"])
    assert res.pred_lo["p"] < res.target_pred["p"] < res.pred_hi["p"]
    assert res.pred_lo["pppp"] < res.pred_hi["pppp"]


def test_span_model_calibration_smoke():
    df = _toy_panel(16)
    model = fit_log_span_model(df, calibrate=True)
    assert model is not None
    assert model.meas_ratio > 0
    assert model.cov_scale > 0
    cal = calibrate_log_span_model(df, model=model, n_draws=80, seed=1)
    assert cal["n"] >= 10
    assert 0.0 <= cal["coverage_D_lo"] <= 1.0
    assert "coverage_mf_pushforward" in cal


def test_pushforward_anchors_exact():
    df = _toy_panel()
    model = fit_log_span_model(df, calibrate=True)
    lo, hi, meta = pushforward_predictive_intervals(
        _mono_anchors(), model, octave=4, n_draws=100, seed=2
    )
    assert lo["mf"] == hi["mf"] == pytest.approx(12.0)
    assert meta["interval_kind"] == INTERVAL_KIND


def test_conjugate_posterior_shrinks_toward_register_mean():
    df = _toy_panel(20)
    model = fit_log_span_model(df, calibrate=False, meas_ratio=0.4, cov_scale=1.0)
    assert model is not None
    y = np.array([0.5, 0.5])
    mu_post, cov_post = model.posterior(y, octave=4)
    mu_r = model.mu_for(4)
    assert np.all((mu_post - mu_r) * (y - mu_post) >= -1e-9) or np.allclose(mu_post, mu_r, atol=1e-6)
    assert cov_post[0, 0] < model.cov[0, 0]


def test_run_transfer_ok_count():
    df = parse_paste_panel("note\tpp\tmf\tff\nG3\t10\t12\t16\nA3\t11\t13\t17\n")
    results = run_transfer(df, use_pchip=False)
    assert len(results) == 2
    assert all(r.status == "ok" for r in results)
    assert set(results[0].target_pred) == set(DYN_ORDER)


def test_place_equal_log_ladder_keys():
    logs = {a: math.log(v) for a, v in _mono_anchors().items()}
    out, audit, warns = place_equal_log_ladder(logs)
    assert set(out) >= set(DYN_ORDER)
    assert "D_lo_log" in audit
    assert any("equal_log" in w for w in warns)


def test_bootstrap_smoke():
    df = parse_paste_panel(
        "\n".join(["note\tpp\tmf\tff"] + [f"N{i}\t{10+i}\t{12+i}\t{16+i}" for i in range(8)])
    )
    boot = bootstrap_diagnostics(df, n_boot=30, seed=1)
    assert boot["n_boot"] == 30
    assert "hold_mf_rel_mae" in boot["ci"]
    assert boot["ci"]["hold_mf_rel_mae"]["p025"] <= boot["ci"]["hold_mf_rel_mae"]["p975"]


def test_header_map_prefers_exact_pp_over_pianissimo_alias():
    row = (
        "notas",
        "fortissimo",
        "forte",
        "meio forte",
        "meio piano",
        "piano",
        "pianissimo",
        None,
        None,
        "pp",
        "mf",
        "ff",
    )
    hm = _detect_header_map(row)
    assert hm is not None
    assert hm["pp"] == 9
    assert hm["mf"] == 10
    assert hm["ff"] == 11
    assert hm["note"] == 0


def test_package_version_export():
    import dynamics_predicter as dp

    assert dp.__version__ == "1.4.0"


def test_acoustics_prior_keeps_anchors_and_cites_rossing():
    from dynamics_predicter.transfer import ACOUSTICS_LITERATURE_RULES

    res = transfer_one_note(_mono_anchors(10.0, 12.0, 16.0), note="A4", note_target="A4")
    for a in ANCHORS:
        assert res.target_pred_acoustics[a] == pytest.approx(res.target_measured[a])
    assert "R6_anchors_inviolable" in res.acoustics_rules_applied
    assert any(
        "Rossing" in r.get("authors_year", "") or "Science of String" in r.get("work", "")
        for r in ACOUSTICS_LITERATURE_RULES
    )
    bad = transfer_one_note({"pp": 12.0, "mf": 10.0, "ff": 16.0}, note="A4", note_target="A4")
    assert bad.target_pred_acoustics["pp"] == pytest.approx(12.0)
    assert bad.target_pred_acoustics["mf"] == pytest.approx(10.0)
    assert bad.target_pred_acoustics["ff"] == pytest.approx(16.0)


def test_export_writes_acoustics_sheets(tmp_path):
    df = parse_paste_panel("note\tpp\tmf\tff\nA4\t10\t12\t16\nB4\t11\t13\t17\n")
    results = run_transfer(df, use_pchip=False)
    out = tmp_path / "out.xlsx"
    export_excel(results, [], out, meta={"version": "test"})
    xl = pd.ExcelFile(out)
    assert "Results_acoustics_prior" in xl.sheet_names
    assert "Acoustics_prior_rules" in xl.sheet_names
    rules = pd.read_excel(out, sheet_name="Acoustics_prior_rules")
    assert rules["rule_id"].astype(str).str.startswith("R").any()
    assert rules["work"].astype(str).str.contains("Science of String", case=False).any()


def test_ordinario_not_mistaken_for_dynamics_panel():
    assert not is_dynamics_panel_filename("VIOLIN_Zenodo_collections_Arco_ordinario.xlsx")
    assert is_dynamics_panel_filename("Violino_dinámicas.xlsx")
    assert is_dynamics_panel_filename("Violino_dinamicas.xlsx")


def test_discover_panel_skips_zenodo(tmp_path):
    (tmp_path / "VIOLIN_Zenodo_collections_Arco_ordinario.xlsx").write_bytes(b"PK\x03\x04fake")
    good = tmp_path / "Violino_dinamicas.xlsx"
    good.write_bytes(b"PK\x03\x04fake")
    found = discover_panel_xlsx(tmp_path)
    assert found is not None
    assert found.name == "Violino_dinamicas.xlsx"


def test_explicit_column_map_loads_dinamicas_panel():
    from dynamics_predicter.transfer import ColumnMap, load_panel_xlsx
    from pathlib import Path

    panel = discover_panel_xlsx(Path(r"C:\Users\lmr20\Desktop\Violino - extrapol"))
    if panel is None:
        pytest.skip("no local dynamics panel")
    cmap = ColumnMap(note=0, note_target=8, pp=9, mf=10, ff=11, data_start_row=2)
    df = load_panel_xlsx(panel, column_map=cmap)
    assert len(df) >= 40
    assert df.iloc[0]["tgt_pp"] > 0
    assert abs(float(df.iloc[0]["tgt_mf"]) - 61.659) < 0.1 or "G3" in str(df.iloc[0]["note"])
