# Dynamics_predicter (v1.4)

**IOWA + ORCHIDEA only** — Philharmonia removed.

Installable research package for a 10-level dynamic ladder from measured anchors `pp`, `mf`, `ff`:

`pppp → ppp → pp → p → mp → mf → f → ff → fff → ffff`

| Kind | Levels | Claim strength |
|------|--------|----------------|
| Measured | `pp`, `mf`, `ff` | Strong (library values) |
| Interpolated | `p`, `mp`, `f` | Model-derived |
| Extrapolated | `pppp`, `ppp`, `fff`, `ffff` | Weak (see Sensitivity_outer) |

Bibliographic titles: **[LITERATURE.md](LITERATURE.md)**.

## Install

```bash
cd Dynamics_predicter
pip install -e ".[dev]"
```

## Method

1. Header-aware load of IOWA+ORCHIDEA `pp` / `mf` / `ff`.
2. Equal-log fractions inside segments (`p` ⅓, `mp` ⅔, `f` ½); optional PCHIP (default off).
3. Outer levels via segment log-steps (+ light corpus pooling when spans are tiny).
4. **Intervals:** hierarchical Gaussian on \(\theta=(D_{\mathrm{lo}},D_{\mathrm{hi}})\):
   - \(\theta\mid r\sim\mathcal{N}(\mu_r,\Sigma_{\mathrm{within}})\)
   - \(y\mid\theta\sim\mathcal{N}(\theta,\Sigma_{\mathrm{meas}})\)
   - conjugate posterior pushforward through equal-log
   - `cov_scale` / `meas_ratio` calibrated to nominal LOO coverage
5. `Sensitivity_outer` (±20% steps) is **not** a CI.
6. Aligned hold-outs + baselines; bootstrap on corpus ratios / hold-out MAE.

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

Primary: `START_HERE`, green **`Results`** (data-faithful).

Literature second track: purple **`Results_acoustics_prior`** + **`Acoustics_prior_rules`** (R0–R6; primary book Rossing *The Science of String Instruments*).

Also: `Predictions_10dyn`, `Measured_anchors`, `Interpolated`, `Extrapolated`, `Quality_flags`, `Holdout_*`, `Bootstrap_CI`, `Interval_calibration`, `Sensitivity_outer`, `Limitations`, `Literature`, `Run_meta`.
