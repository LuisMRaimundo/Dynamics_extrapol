# CHANGES

## v1.5.2.1 — reconcile non-monotone accounting

- Tanh guard refusal reasons are two distinct strings:
  - ``non-monotonic anchors`` — strict opposite log-span signs (both
    \(|\mathrm{span}|\ge\) `TANH_NEAR_FLAT_TOL`);
  - ``near-flat segment (|span| < tol): saturating fit ill-posed`` —
    exactly one near-flat segment (`TANH_NEAR_FLAT_TOL = 1e-12`,
    internal_default).
- Shared classifier `classify_anchor_log_spans` also drives the quality flag
  `nonmonotonic_anchors`, so the definitions cannot diverge.
- `Run_meta`: `n_tanh_na_nonmono`, `n_tanh_na_flat`,
  `n_tanh_na = sum (+ other N/A)`; **assert**
  `n_tanh_na_nonmono == n_nonmonotonic_anchors` in `build_run_meta` and again
  at Excel export.
- Test: one strict-opposite + one near-flat triple → the two distinct reasons.

## v1.5.2 — tanh applicability guard

- `place_tanh_saturating_ladder`: before fitting, require same-sign
  non-flat spans. Opposite signs / one-sided near-flat → anchors kept,
  non-anchor levels **NaN** (no fit, no corrective re-pin). See v1.5.2.1
  for the two distinct reason strings.
- After evaluation, **verify** the raw curve hits all three anchors within
  `1e-6` in log space; failure → same NaN path
  (`tanh_na_anchor_verification_failed`). Re-pinning absorbs float dust only.
- `Results_tanh`: N/A non-anchor cells blank + per-row `tanh_note`;
  `Run_meta.n_tanh_na`; `START_HERE` states the tanh track covers
  **monotone-anchor notes only by design**.
- Tests: rise–fall fixture → NaN + warn; monotone fixture bit-identical to
  v1.5.1; collinear fixture still takes the linear-limit path.

## v1.5.1 — coherent multi-dynamic panel intake

- Accepts upstream `extrapol_data --fill-panel` provenance (`prov_pp/mf/ff`,
  `edge_filled_anchor`) on the panel workbook.
- Rows with `edge_filled` anchors get a quality malus (`EDGE_FILLED_QUALITY_MALUS`)
  and flag `edge_filled_anchor`, propagated to `Results`, `Quality_flags`,
  `Measured_anchors`, and SDA `kind_*` (`edge_filled_anchor` /
  `interior_filled_anchor` vs `measured_anchor`).

## v1.5.0 — curvature nuance (log space; no new "log" layer)

Preserves the public API surface (`transfer_one_note`, `run_transfer`, `run_pipeline`,
`export_excel`, CLI flags), the anchors-inviolable rule (**R6**), and all existing
diagnostics (hold-outs, bootstrap, LOO, pushforward intervals, Sensitivity_outer).

### 1. Tapered outers (R7)

- In `place_equal_log_ladder` and `acoustics_regularize_ladder`, successive outer
  log-steps are `step · r^(k−1)` (cumulative geometric sum for k=1 → ppp/fff,
  k=2 → pppp/ffff).
- Internal default: `OUTER_TAPER_R = 0.80`.
- **`r = 1.0` reproduces v1.4 bit-exactly** (dedicated branch with the old
  `step` / `2·step` expressions).
- Cited in `ACOUSTICS_LITERATURE_RULES` as **R7_outer_taper_compression**
  (Meyer 2009 dynamic-range compression + Patterson 1974 auditory compression).
- **R3** soft-cap remains the final guard; when the taper alone keeps outers
  inside the cap, the acoustics notes log `R3: taper alone sufficed`.

### 2. Sigmoid / tanh track (third sheet)

- New `ladder_mode="tanh_saturating"`:
  `log y = a + b·tanh(c·(idx − idx0))` through the three anchors.
- Fit: primary `idx0 = idx(mf)` ⇒ `a = log(mf)`; 1-D Brent root for `c` from
  collinearity; `b` closed-form. Fallback: free `idx0` search if mf-centered
  ratio is outside the tanh range. Documented on `place_tanh_saturating_ladder`.
- Exposed as sheet **`Results_tanh` only** — never the default `Results` sheet.
- Anchors exact when applicable (v1.5.2: verify ≤1e-6 then dust re-pin; N/A if not).

### 3. Default interiors

- **GUI:** `use_pchip` default **True**.
- **CLI:** `--pchip` remains opt-in (default False).
- `START_HERE` records which sheet used which geometry for the run.

### 4. Sensitivity

- `Sensitivity_outer` gains columns `pred_r0.7`, `pred_r0.8`, `pred_r0.9`,
  `pred_r1.0` so the taper choice is visibly bounded (alongside the existing
  ±20% step bands).

### 5. Tests (see `tests/test_dynamic_shape_transfer.py`)

- Ladder **hygiene** (not global soft→loud monotone): imputed interiors stay in
  their measured segment; extrapolated outers may not move *against* the last
  measured segment by more than the workbook outer step. Measured `pp`/`mf`/`ff`
  are exempt / inviolable (`value_kind` distinguishes cells).
- Taper only shrinks `|outer − anchor|` (never grows) vs `r=1.0`.
- Tanh track reproduces anchors to `1e-9`.
- Pushforward intervals still bracket the point ladder per level.
- `r=1.0` bit-exact vs the v1.4 outer formulas.

### Epistemic note (EWSD / CDM dynamics)

The EWSD-like metric is **empirically non-monotone in dynamics** for bowed
strings (cello: 33/49 notes with ff below mf; median \(R_{\mathrm{ff}/\mathrm{mf}}\approx 0.94\)).
Soft→loud monotonicity is a **hygiene rule for broken extrapolation** on
model-derived cells, not a physical law and never a reason to overwrite anchors.
`Results` now exports `kind_*` columns for downstream SDA import.
