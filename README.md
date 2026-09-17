# Dynamics_predicter (v1.5.2.1)

**IOWA + ORCHIDEA only** — Philharmonia removed.

Installable research package for a 10-level dynamic ladder from measured anchors `pp`, `mf`, `ff`:

`pppp → ppp → pp → p → mp → mf → f → ff → fff → ffff`

| Kind | Levels | Claim strength |
|------|--------|----------------|
| Measured | `pp`, `mf`, `ff` | Strong (library values) |
| Interpolated | `p`, `mp`, `f` | Model-derived |
| Extrapolated | `pppp`, `ppp`, `fff`, `ffff` | Weak (see Sensitivity_outer) |

**CDM / EWSD is not soft→loud monotone in the wild** (cello: 33/49 notes with
ff&lt;mf; median \(R_{\mathrm{ff}/\mathrm{mf}}\approx 0.94\)). Hygiene tests cover
imputed/extrapolated cells only; measured anchors stay inviolable (`kind_*` on
`Results`).

See **[CHANGES.md](CHANGES.md)** for v1.5. Bibliographic titles: **[LITERATURE.md](LITERATURE.md)**.

## Install

```bash
cd Dynamics_predicter
pip install -e ".[dev]"
```

## Method

1. Header-aware load of IOWA+ORCHIDEA `pp` / `mf` / `ff`.
2. Equal-log fractions inside segments (`p` ⅓, `mp` ⅔, `f` ½); optional PCHIP (**GUI default on**, CLI `--pchip` opt-in).
3. Outer levels via tapered segment log-steps `step·r^(k−1)` (default `r=0.80`; `r=1.0` = v1.4) + light corpus pooling when spans are tiny.
4. **Intervals:** hierarchical Gaussian on \(\theta=(D_{\mathrm{lo}},D_{\mathrm{hi}})\):
   - \(\theta\mid r\sim\mathcal{N}(\mu_r,\Sigma_{\mathrm{within}})\)
   - \(y\mid\theta\sim\mathcal{N}(\theta,\Sigma_{\mathrm{meas}})\)
   - conjugate posterior pushforward through equal-log
   - `cov_scale` / `meas_ratio` calibrated to nominal LOO coverage
5. `Sensitivity_outer` (±20% steps **and** `r∈{0.7,0.8,0.9,1.0}`) is **not** a CI.
6. Aligned hold-outs + baselines; bootstrap on corpus ratios / hold-out MAE.
7. Third track `Results_tanh`: `log y = a + b·tanh(c·(idx−idx0))` — never the default.

## Run

```bash
python -m dynamics_predicter
dynamics-predicter --n-boot 200
dynamics-predicter-gui
python -m pytest -q
```

Legacy shims (still work): `python dynamic_shape_transfer.py`, `start.bat`.

## CI

GitHub Actions: `.github/workflows/ci.yml` — pytest on Python 3.10–3.12.

## Output sheets

Primary: `START_HERE`, green **`Results`** (data-faithful equal-log + taper).

Literature second track: purple **`Results_acoustics_prior`** + **`Acoustics_prior_rules`** (R0–R7; primary book Rossing *The Science of String Instruments*).

Third track: terracotta **`Results_tanh`** (`ladder_mode=tanh_saturating`; never default).

Also: `Predictions_10dyn`, `Measured_anchors`, `Interpolated`, `Extrapolated`, `Quality_flags`, `Holdout_*`, `Bootstrap_CI`, `Interval_calibration`, `Sensitivity_outer`, `Limitations`, `Literature`, `Run_meta`.
