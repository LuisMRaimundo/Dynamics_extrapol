"""Synthetic validation for documented contracts (no research-corpus I/O)."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from dynamics_predicter.transfer import (
    ANCHORS,
    DYN_ORDER,
    EXTRAPOLATED_BEYOND,
    INTERPOLATED,
    _norm_ppf,
    _stable_note_seed_offset,
    fit_log_span_model,
    parse_paste_panel,
    place_equal_log_ladder,
    pushforward_predictive_intervals,
    run_pipeline,
    transfer_one_note,
    validate_panel_df,
)


def _mono(pp=10.0, mf=12.0, ff=16.0) -> dict[str, float]:
    return {"pp": pp, "mf": mf, "ff": ff}


def _toy_panel(n: int = 12) -> pd.DataFrame:
    rows = []
    for i in range(n):
        pp = 8.0 + i * 0.5
        mf = pp * 1.15
        ff = mf * 1.12
        rows.append(
            {
                "note": f"C{4 + (i % 3)}",
                "note_target": f"C{4 + (i % 3)}{i}",
                "tgt_pp": pp,
                "tgt_mf": mf,
                "tgt_ff": ff,
            }
        )
    return pd.DataFrame(rows)


def test_stable_note_offset_independent_of_pythonhashseed():
    code = (
        "from dynamics_predicter.transfer import _stable_note_seed_offset; "
        "print(_stable_note_seed_offset('C4'))"
    )
    offsets = []
    for hs in ("0", "1", "random"):
        env = {**os.environ, "PYTHONHASHSEED": hs}
        out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
        offsets.append(int(out.strip()))
    assert offsets[0] == offsets[1] == offsets[2]
    assert _stable_note_seed_offset("C4") == offsets[0]
    assert _stable_note_seed_offset("C4") != _stable_note_seed_offset("D4")


def test_interval_bounds_reproducible_across_hash_seeds():
    """pred_lo/pred_hi must not move when PYTHONHASHSEED changes."""
    code = r"""
from dynamics_predicter.transfer import fit_log_span_model, transfer_one_note
import pandas as pd
rows=[]
for i in range(12):
    pp=8.0+i*0.5; mf=pp*1.15; ff=mf*1.12
    rows.append({'note':f'C{4+(i%3)}','note_target':f'C{4+(i%3)}','tgt_pp':pp,'tgt_mf':mf,'tgt_ff':ff})
df=pd.DataFrame(rows)
model=fit_log_span_model(df)
res=transfer_one_note({'pp':10.0,'mf':11.5,'ff':12.88}, span_model=model, note_target='C4', seed=20260801, n_pushforward=80)
print(res.pred_lo['p'], res.pred_hi['p'], res.pred_lo['pppp'], res.pred_hi['ffff'])
"""
    outs = []
    for hs in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": hs}
        outs.append(
            subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip()
        )
    assert outs[0] == outs[1]


def test_same_seed_same_intervals_in_process():
    df = _toy_panel()
    model = fit_log_span_model(df)
    kwargs = dict(
        tgt_anchors=_mono(10.0, 11.5, 12.88),
        span_model=model,
        note_target="C4",
        seed=20260801,
        n_pushforward=120,
    )
    a = transfer_one_note(**kwargs)
    b = transfer_one_note(**kwargs)
    for d in DYN_ORDER:
        assert a.pred_lo[d] == pytest.approx(b.pred_lo[d], abs=0.0, rel=0.0)
        assert a.pred_hi[d] == pytest.approx(b.pred_hi[d], abs=0.0, rel=0.0)


def test_norm_ppf_matches_scipy_on_nominal_interval():
    for p in (0.025, 0.05, 0.10, 0.50, 0.90, 0.95, 0.975):
        assert _norm_ppf(p) == pytest.approx(float(norm.ppf(p)), rel=0, abs=2e-8)
    assert math.isinf(_norm_ppf(0.0))
    assert math.isinf(_norm_ppf(1.0))


def test_constant_anchors_finite_ladder():
    res = transfer_one_note(_mono(5.0, 5.0, 5.0), use_pchip=False)
    assert res.status == "ok"
    for d in DYN_ORDER:
        assert res.target_pred[d] == pytest.approx(5.0, rel=0, abs=1e-12)
    assert "near_zero_total_span" in res.flags or "flat_dynamic_span" in res.flags


def test_nonpositive_anchors_skipped():
    res = transfer_one_note({"pp": 0.0, "mf": 2.0, "ff": 3.0})
    assert res.status == "skipped_target"
    assert "incomplete_anchors" in res.flags


def test_paste_rejects_zero_and_nan_tokens():
    with pytest.raises(ValueError):
        parse_paste_panel("G3\t0\t2\t3\n")
    with pytest.raises(ValueError):
        parse_paste_panel("G3\t1\tnan\t3\n")


def test_log_exp_anchor_roundtrip():
    tgt = _mono(7.5, 11.0, 19.25)
    res = transfer_one_note(tgt, use_pchip=False)
    for a in ANCHORS:
        assert res.target_pred[a] == pytest.approx(tgt[a], abs=0.0, rel=0.0)
        assert math.exp(math.log(tgt[a])) == pytest.approx(tgt[a])


def test_pchip_changes_interiors_not_anchors():
    eq = transfer_one_note(_mono(10.0, 12.0, 16.0), use_pchip=False)
    pc = transfer_one_note(_mono(10.0, 12.0, 16.0), use_pchip=True)
    for a in ANCHORS:
        assert pc.target_pred[a] == pytest.approx(eq.target_pred[a], abs=0.0)
    # Non-collinear in log-index: PCHIP need not match 1/3–2/3–1/2 fractions
    assert any(
        abs(pc.target_pred[d] - eq.target_pred[d]) > 1e-12 for d in INTERPOLATED
    )


def test_equal_log_is_modeling_convention_not_physical_spacing():
    """Indices 0..9 are equally spaced by construction; this is a convention."""
    from dynamics_predicter.transfer import DYN_LEVEL

    idxs = [DYN_LEVEL[d] for d in DYN_ORDER]
    steps = np.diff(idxs)
    assert np.allclose(steps, 1.0)
    # Equal index steps do not imply equal measured ratios
    tgt = _mono(10.0, 12.0, 40.0)
    logs = {a: math.log(v) for a, v in tgt.items()}
    assert abs((logs["mf"] - logs["pp"]) - (logs["ff"] - logs["mf"])) > 0.2


def test_validate_duplicates_but_pipeline_still_runs_on_unique_synthetic(tmp_path):
    df = parse_paste_panel("note\tpp\tmf\tff\nA4\t10\t12\t16\nA4\t11\t13\t17\n")
    errs = validate_panel_df(df)
    assert any("duplicate" in e for e in errs)
    # Unique notes: pipeline writes only to the external workbook
    unique = parse_paste_panel(
        "\n".join(["note\tpp\tmf\tff"] + [f"N{i}\t{8+i}\t{10+i}\t{14+i}" for i in range(8)])
    )
    out = tmp_path / "synth_pipeline.xlsx"
    results, holdouts, bootstrap, loo, meta = run_pipeline(
        unique, source="synthetic", use_pchip=False, n_boot=20, seed=7
    )
    from dynamics_predicter.transfer import export_excel, outer_sensitivity_table

    export_excel(
        results,
        holdouts,
        out,
        meta,
        sensitivity_df=outer_sensitivity_table(results, unique),
        calibration=meta.get("span_model_calibration_loo"),
    )
    assert out.is_file()
    assert meta["n_ok"] == 8
    assert bootstrap["n_boot"] == 20
    assert loo["n"] == 8


def test_missing_anchor_rows_skipped_in_run_transfer():
    from dynamics_predicter.transfer import run_transfer

    df = pd.DataFrame(
        [
            {"note": "A4", "note_target": "A4", "tgt_pp": 10.0, "tgt_mf": 12.0, "tgt_ff": 16.0},
            {"note": "B4", "note_target": "B4", "tgt_pp": 10.0, "tgt_mf": np.nan, "tgt_ff": 16.0},
        ]
    )
    results = run_transfer(df, use_pchip=False)
    assert results[0].status == "ok"
    assert results[1].status == "skipped_incomplete"


def test_pushforward_does_not_claim_measurement_noise_on_anchors():
    df = _toy_panel()
    model = fit_log_span_model(df, calibrate=False)
    lo, hi, meta = pushforward_predictive_intervals(
        _mono(), model, octave=4, n_draws=80, seed=3
    )
    assert lo["pp"] == hi["pp"] == pytest.approx(10.0)
    assert meta["interval_kind"] == "eb_gaussian_posterior_pushforward"
    # Point ladder uses measured anchors; bands on anchors are overwritten exact
    logs = {a: math.log(v) for a, v in _mono().items()}
    point, _, _ = place_equal_log_ladder(logs)
    assert math.exp(point["pp"]) == pytest.approx(10.0)
