# Dynamics_extrapol — mathematical reference

**Status:** production documentation of the implementation on `fix/dynamics-validation-and-documentation` (v1.5.2.2).  
**Language:** English. StackEdit-compatible Markdown + LaTeX (`$...$` inline, `$$...$$` display). No custom macros.

This document extracts **project-defined** mathematics from first-party Python. External-library internals are **not** reproduced; only the call site, arguments, and surrounding project math are recorded.

**This document does not certify scientific validity.** Synthetic tests and documentation are not a substitute for a labelled research-corpus study. Historical IOWA/ORCHIDEA analyses were **not** regenerated.

---

## Implementation baseline

| Item | Value |
|------|--------|
| GitHub pin at alignment | `2344e5ae050edf0bff4f1c11eb711f3c61f41abe` |
| Review branch | `fix/dynamics-validation-and-documentation` |
| Isolated environment | Python 3.10.11; `numpy 2.2.6`, `pandas 2.3.3`, `scipy 1.15.3`, `openpyxl 3.1.5`, `pytest 9.1.1` |
| Logarithm | natural log `numpy.log` / `numpy.exp` (not $\log_{10}$, not dB) |
| Default taper | $r=0.80$; $r=1$ is the v1.4 equal-step path |
| Interval label | `eb_gaussian_posterior_pushforward`, $\alpha=0.10$ (nominal 90%) |
| RNG default | seed $20260801$ |

Hashes describe the Python sources **after** the v1.5.2.2 seed fix. Recompute if those files change.

| File | SHA-256 | Lines | Coverage |
|------|---------|------:|----------|
| `dynamics_predicter/transfer.py` | `405780fd9a188d07eb3c923341b9b0eb614e874c7948312ba2875be503767cdb` | 3391 | All production math |
| `dynamics_predicter/gui.py` | `53385f6b9bdab41c06d86baa236c94992eef7c9330830bc92f9d66244a0e881d` | 495 | Calls `run_pipeline` / `export_excel` |
| `dynamics_predicter/__init__.py` | `66a9882cb4f24e7366057604fec7465a41bc9a5814019476e1b6c812367f5810` | 29 | Re-exports |
| `dynamics_predicter/__main__.py` | `db5e2ec8ca133a8418d993ccfaf42705a199dae459eca8e9fb971bfa7c766c95` | 3 | `main()` |
| `dynamic_shape_transfer.py` | `0e03f0f71dea4d6dc493479595e6fea675ed4222ba9bed6dab34c52ffd82de17` | 7 | Legacy shim |
| `run_gui.py` | `5e87aeed4456c5b937aaf604ad025afbf5dd024703258b16498e1a60d4093f98` | 6 | Legacy GUI shim |
| `run_all_instruments_batch.py` | `64f2dac417a9d0a4785df3c81b1913977d0cb289d5e1a3f89a79ac5de85f030a` | 348 | Offline local batch (no new ladder law) |
| `run_para_dinamicas_folder.py` | `477f58b82bdfa13863448d3b3b204162e362d532900c874511eb4d4f4576d0c3` | 162 | Offline local batch |

Tests contain no independent production formulae. `outputs/compare_tracks.json` is committed comparison data, not a generator. No notebooks are in this repository.

Working-tree note at documentation time: review edits on the fix branch; unpublished ignored caches only.

---

## What a production run computes

| Quantity | Source | Role |
|----------|--------|------|
| Anchors $y_{\mathrm{pp}},y_{\mathrm{mf}},y_{\mathrm{ff}}$ | Excel / paste, strictly $>0$ | Only measured values on `Results` |
| Index $\mathrm{idx}(d)$ | `DYN_LEVEL` | Integer $0\ldots9$ for `pppp`$\ldots$`ffff` — **convention** |
| Log spans $D_{\mathrm{lo}},D_{\mathrm{hi}}$ | $\log y_{\mathrm{mf}}-\log y_{\mathrm{pp}}$, $\log y_{\mathrm{ff}}-\log y_{\mathrm{mf}}$ | Geometry + hierarchical model |
| Point ladder | `place_equal_log_ladder` ± optional PCHIP interiors | Default `Results` |
| Acoustics track | `acoustics_regularize_ladder` | Second sheet; equal-log interiors always |
| Tanh track | `place_tanh_saturating_ladder` | Third sheet; N/A if non-monotone / one-sided flat |
| Interval bands | posterior draws of $\theta$, pushforward | Imputed/outer cells only |

There is **no** Gaussian-process kernel, **no** dB conversion, and **no** committed lookup table of predicted dynamics. Runtime evaluates the formulae below.

---

## Project formulae

### M-001 — Dynamic-label index encoding (production)

**Source:** `dynamics_predicter/transfer.py`, `DYN_ORDER` / `DYN_LEVEL`, lines 48–49.

```python
DYN_ORDER = ("pppp", "ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "ffff")
DYN_LEVEL = {d: float(i) for i, d in enumerate(DYN_ORDER)}
```

$$\mathrm{idx}(\texttt{pppp})=0,\ \ldots,\ \mathrm{idx}(\texttt{ffff})=9$$

**Symbols:** $d$ dynamic label; $\mathrm{idx}(d)\in\{0,\ldots,9\}$. Units: dimensionless index. Shape: scalar per label.

**Layman:** The ten names are lined up as ten equally spaced steps.

**Specialist:** Equal integer spacing is a **modeling convention**. It is not an empirical calibration of musical dynamic marks to amplitude, power, or SPL. Segment fractions (M-008) live in **log-metric** space using **measured** $D_{\mathrm{lo}},D_{\mathrm{hi}}$, not in this index except for PCHIP / tanh.

**Assumptions:** Order is fixed; there is no user remapping.

**Downstream:** PCHIP abscissae (M-015); tanh argument (M-012). Tests: `test_equal_log_is_modeling_convention_not_physical_spacing`.

---

### M-002 — Natural-log spans (production)

**Source:** `_complete_anchor_rows` lines 321–322; `target_distance_geometry` lines 1347–1358.

```python
        d_lo = float(np.log(mf) - np.log(pp))
        d_hi = float(np.log(ff) - np.log(mf))
```

$$D_{\mathrm{lo}}=\log y_{\mathrm{mf}}-\log y_{\mathrm{pp}},\qquad D_{\mathrm{hi}}=\log y_{\mathrm{ff}}-\log y_{\mathrm{mf}}$$

$$R_{\mathrm{mf}/\mathrm{pp}}=e^{D_{\mathrm{lo}}},\quad
R_{\mathrm{ff}/\mathrm{mf}}=e^{D_{\mathrm{hi}}},\quad
R_{\mathrm{ff}/\mathrm{pp}}=e^{D_{\mathrm{lo}}+D_{\mathrm{hi}}}$$

$$\mathrm{share}_{\mathrm{lo}}=\frac{D_{\mathrm{lo}}}{D_{\mathrm{lo}}+D_{\mathrm{hi}}}\quad\text{if }|D_{\mathrm{lo}}+D_{\mathrm{hi}}|>10^{-12},\ \text{else }1/2$$

**Symbols:** $y$ positive recorded metric (EWSD-like / CDM score as stored — **not** SPL). $\log$ is natural. Domain: $y>0$.

**Layman:** How many log-steps from pp to mf, and from mf to ff.

**Specialist:** Additive geometry on $\log y$. Signs may be negative (empirically common: $\mathrm{ff}<\mathrm{mf}$). `share_lo` is a **log-span share**, not a linear-loudness share.

**Undefined:** $y\le 0$ rows are dropped (M-003).

**Downstream:** hierarchical model (M-006); equal-log interiors (M-008); hold-out (M-023). Tests: `test_equal_log_fractions_interior`.

---

### M-003 — Complete-anchor filter (production)

**Source:** `_complete_anchor_rows` lines 309–324.

Keep a row iff all three `tgt_*` are present and $\min(y_{\mathrm{pp}},y_{\mathrm{mf}},y_{\mathrm{ff}})>0$.

**Downstream:** model fit requires $n\ge 5$ (`MIN_MODEL_N`).

---

### M-004 — Symmetric positive-definite covariance floor (production)

**Source:** `_psd` lines 327–335.

```python
    c = 0.5 * (c + c.T) + np.eye(2) * COV_RIDGE
    w, v = np.linalg.eigh(c)
    w = np.maximum(w, COV_RIDGE)
    return (v * w) @ v.T
```

With `COV_RIDGE = 1e-6`: symmetrize, add $\lambda I$, floor eigenvalues at $\lambda$.

**Layman:** Make the $2\times 2$ covariance usable for inversion.

**Specialist:** Project-defined ridge; library `eigh` is L-003.

---

### M-005 — Register partial pooling of span means (production)

**Source:** `fit_log_span_model` lines 365–370. `SPAN_SHRINK_N0 = 0.35`.

$$\mu_r=\frac{n_r\,\bar{y}_r+n_0\,\mu_g}{n_r+n_0},\qquad n_0=0.35$$

**Symbols:** $\bar{y}_r$ sample mean of $(D_{\mathrm{lo}},D_{\mathrm{hi}})$ in octave $r$; $\mu_g$ global mean; $n_r$ count. Octave from trailing digits of the note (M-029).

**Layman:** Small registers are pulled toward the whole-panel average.

**Specialist:** Gelman–Hill-style shrinkage, **not** a full multilevel posterior. Residuals used for $\Sigma_{\mathrm{within}}$ are $y-\mu_r$ **after** shrinkage (slight downward bias of within variance — modeling choice).

**Downstream:** `LogSpanModel.mu_for`. Tests: `test_conjugate_posterior_shrinks_toward_register_mean`.

---

### M-006 — Hierarchical Gaussian on log-spans (production)

**Source:** `LogSpanModel` docstring lines 231–239; `fit_log_span_model` 338–404.

$$\theta_i\mid r_i\sim\mathcal{N}(\mu_{r_i},\Sigma_{\mathrm{within}}),\qquad
y_i\mid\theta_i\sim\mathcal{N}(\theta_i,\Sigma_{\mathrm{meas}})$$

$$\Sigma_{\mathrm{meas}}=\rho\,\Sigma_{\mathrm{within}},\qquad \rho=\texttt{meas\_ratio}\ (\mathrm{default}\ 0.35)$$

Scaled prior used at interval time: $\Sigma_0=\texttt{cov\_scale}\cdot\Sigma_{\mathrm{within}}$.

$\tau^2_{\mathrm{between}}$ is the mean of the two-component sample variances of raw register means when $\ge 2$ registers exist; **not** used in the posterior formula.

**If** $n<5$: no model; intervals collapse to the point ladder.

**Downstream:** M-007, M-020. Label `INTERVAL_KIND`.

---

### M-007 — Conjugate Gaussian posterior of $\theta$ (production)

**Source:** `LogSpanModel.posterior` lines 268–281.

```python
        prec0 = np.linalg.inv(s0)
        precm = np.linalg.inv(sm)
        prec_post = prec0 + precm
        cov_post = np.linalg.inv(prec_post)
        mu_post = cov_post @ (prec0 @ mu0 + precm @ np.asarray(y_obs, dtype=float))
```

$$\Sigma_{\mathrm{post}}=(\Sigma_0^{-1}+\Sigma_m^{-1})^{-1},\qquad
\mu_{\mathrm{post}}=\Sigma_{\mathrm{post}}(\Sigma_0^{-1}\mu_0+\Sigma_m^{-1}y)$$

Then $\Sigma_{\mathrm{post}}\leftarrow(\Sigma_{\mathrm{post}}+\Sigma_{\mathrm{post}}^\top)/2$.

**Layman:** Blend the note’s observed spans with the register prior.

**Specialist:** Standard normal–normal conjugate update. This is **uncertainty in latent spans**, not a new observation $y_{\mathrm{new}}$.

**Tests:** `test_conjugate_posterior_shrinks_toward_register_mean`.

---

### M-008 — Equal-log interior fractions (production default interiors)

**Source:** constants 54–55; `place_equal_log_ladder` 1434–1439.

```python
    for d, frac in UNIFORM_FRAC_LO.items():
        out[d] = tgt_log["pp"] + frac * d_lo
    for d, frac in UNIFORM_FRAC_HI.items():
        out[d] = tgt_log["mf"] + frac * d_hi
```

$$\log y_p=\log y_{\mathrm{pp}}+\tfrac13 D_{\mathrm{lo}},\quad
\log y_{\mathrm{mp}}=\log y_{\mathrm{pp}}+\tfrac23 D_{\mathrm{lo}},\quad
\log y_f=\log y_{\mathrm{mf}}+\tfrac12 D_{\mathrm{hi}}$$

Then $y=\exp(\cdot)$.

**Layman:** Put p and mp evenly in log-space between pp and mf; put f halfway in log-space between mf and ff.

**Specialist:** The fractions $1/3,2/3,1/2$ match the **count of notated steps** in each measured segment (`N_STEPS_LO=3`, `N_STEPS_HI=2`), not equal perceptual loudness. This is a convention.

**Downstream:** `Results` when `--pchip` is off. Tests: `test_equal_log_fractions_interior`.

---

### M-009 — Corpus-pooled outer step (production)

**Source:** `_pooled_steps` lines 1388–1406.

$$\mathrm{step}_{\mathrm{lo}}=D_{\mathrm{lo}}/3,\quad \mathrm{step}_{\mathrm{hi}}=D_{\mathrm{hi}}/2$$

If corpus $n\ge 3$:

$$w=\mathrm{clip}\bigl(0.35\cdot \tfrac{c_{\mathrm{scale}}}{\mathrm{scale}+c_{\mathrm{scale}}},0,0.85\bigr)$$

$$\mathrm{step}\leftarrow (1-w)\,\mathrm{step}+w\,\mathrm{step}_{\mathrm{corpus}}$$

`scale` $= \max(|D_{\mathrm{lo}}|+|D_{\mathrm{hi}}|,10^{-6})$. Tiny local spans shrink toward corpus median steps.

**Downstream:** outer placement (M-011). LOO interiors without pooling are unchanged (`leave_one_note_out_register` note).

---

### M-010 — Tapered outer cumulative offset (production)

**Source:** `tapered_outer_cum_offset` lines 1409–1421. Default $r=0.80$.

```python
    if r == 1.0:
        return float(step) * float(k)
    return float(step) * (1.0 - float(r) ** k) / (1.0 - float(r))
```

$$\Delta_k=\begin{cases}
k\cdot\mathrm{step} & r=1\\
\mathrm{step}\,(1-r^k)/(1-r) & r\neq 1
\end{cases}
=\sum_{i=1}^{k}\mathrm{step}\,r^{i-1}$$

$k=1$: `ppp`/`fff`; $k=2$: `pppp`/`ffff`. $k\le 0\to 0$.

**Layman:** Each extra outer step is a bit smaller than the last when $r<1$.

**Specialist:** Cited in-code as Meyer / Patterson compression (R7). $r=1$ is bit-exact v1.4. This is a **modeling prior**, not a fitted auditory parameter.

**Tests:** `test_taper_r1_bit_exact_v14`, `test_taper_only_shrinks_outer_distance`.

---

### M-011 — Equal-log 10-level ladder (production default)

**Source:** `place_equal_log_ladder` lines 1447–1462.

Soft side subtracts $\Delta_k(\mathrm{step}_{\mathrm{lo}})$ from $\log y_{\mathrm{pp}}$; loud side adds $\Delta_k(\mathrm{step}_{\mathrm{hi}})$ to $\log y_{\mathrm{ff}}$. Anchors copied exactly.

**Layman:** Continue the pp–mf step below pp, and the mf–ff step above ff, with optional taper.

**Specialist:** If $D_{\mathrm{lo}}<0$, soft outers go **above** pp (continuation of a falling segment). Soft→loud global monotone is **not** imposed. Hygiene tests only restrict **adverse** motion of derived cells (`_ladder_hygiene_errors`).

**Downstream:** `transfer_one_note` → `Results`. Caller: `run_transfer` → `run_pipeline` → CLI `main` / GUI `_export`.

---

### M-012 — Tanh-saturating third track (production, never default)

**Source:** `place_tanh_saturating_ladder` lines 1607–1769.

When applicable:

$$\log y(d)=a+b\,\tanh\bigl(c\,(\mathrm{idx}(d)-\mathrm{idx}_0)\bigr)$$

Primary: $\mathrm{idx}_0=\mathrm{idx}(\mathrm{mf})=5$, hence $a=\log y_{\mathrm{mf}}$. Solve $c$ from collinearity

$$A\tanh(c\,d_{\mathrm{ff}})-B\tanh(c\,d_{\mathrm{pp}})=0$$

with $A=\log y_{\mathrm{pp}}-a$, $B=\log y_{\mathrm{ff}}-a$, $d_{\mathrm{pp}}=-3$, $d_{\mathrm{ff}}=+2$. Then $b=B/\tanh(c\,d_{\mathrm{ff}})$ (or $A/\tanh(c\,d_{\mathrm{pp}})$).

If the linear residual $|A d_{\mathrm{ff}}-B d_{\mathrm{pp}}|<10^{-12}$, use $c=0$:

$$\log y=a+b\,(\mathrm{idx}-\mathrm{idx}_0)$$

Fallback: search $\mathrm{idx}_0\in(\mathrm{idx}_{\mathrm{pp}}+0.05,\mathrm{idx}_{\mathrm{ff}}-0.05)$ (61 points) with a 3-point collinearity residual.

**Verification:** raw curve must hit all three anchors within $10^{-6}$ in log space; else N/A. Re-pin anchors only after that (float dust).

**Layman:** Optional S-shaped curve through the three measured points. Not used for the green `Results` sheet.

**Specialist:** Ill-posed when one segment is near-flat or spans have opposite signs (M-013). Frozen v1.5.1 monotone reference is tested bit-exactly.

**Tests:** `test_tanh_track_reproduces_anchors`, `test_tanh_monotone_bit_identical_v151`, `test_tanh_collinear_linear_limit_path`.

---

### M-013 — Anchor-span classifier (production)

**Source:** `classify_anchor_log_spans` lines 1555–1577. `TANH_NEAR_FLAT_TOL=1e-12`.

- `nonmono`: opposite signs and both $|D|\ge\mathrm{tol}$
- `flat_one_sided`: exactly one $|D|<\mathrm{tol}$
- `ok`: same sign, or both near-flat

Used for tanh N/A reasons **and** the `nonmonotonic_anchors` quality flag (on **log** $y$). A separate flag `opposite_segment_signs` uses $D_{\mathrm{lo}}D_{\mathrm{hi}}<0$ without the flat tolerance (can double-count quality).

**Tests:** `test_tanh_na_distinct_nonmono_and_flat_reasons`. `build_run_meta` asserts $n_{\mathrm{tanh,na,nonmono}}=n_{\mathrm{nonmonotonic}}$.

---

### M-014 — Brent–Dekker root (production helper)

**Source:** `_brent_root` lines 1483–1536.

Project-owned scalar root finder on $[a,b]$ if $f(a)f(b)\le 0$. Returns `None` if no sign change. Used only for tanh $c$.

**Not** a closed form. Library `scipy.optimize` is **not** called.

---

### M-015 — PCHIP interiors (optional production)

**Source:** `pchip_intermediates_from_anchors` lines 1772–1777.

```python
    spline = PchipInterpolator(xs, ys, extrapolate=False)
    return {d: float(spline(DYN_LEVEL[d])) for d in INTERPOLATED}
```

$x=\mathrm{idx}(\mathrm{pp,mf,ff})=(2,5,7)$, $y=\log$ anchors. Evaluate at $p,mp,f$ only. Outers stay M-011.

**Layman:** Smooth monotone cubic through the three logs; only p/mp/f change.

**Specialist:** Fritsch–Carlson PCHIP (L-006). GUI default on; CLI default off. **Acoustics track ignores PCHIP** (always M-008 interiors).

**Tests:** `test_anchors_exact_with_pchip`, `test_pchip_changes_interiors_not_anchors`.

---

### M-016 — Note quality score (production)

**Source:** `note_quality_and_flags` lines 1780–1821.

Start $q=1$. Subtract $0.35$ (`nonmonotonic_anchors`), $0.15$ (`opposite_segment_signs`), $0.25$ (`near_zero_total_span` if $|D_{\mathrm{tot}}|<10^{-6}$), $0.10$ (`flat_dynamic_span` if $\min/\max>0.98$), plus provenance malus $0.20$ / $0.10$. Add $\mathrm{clip}(|D_{\mathrm{tot}}|,0,0.15)$. Clip $q$ to $[0.05,1]$.

**Layman:** A heuristic traffic-light, not a statistical p-value.

**Downstream:** `Quality_flags` sheet; `mean_quality` in `Run_meta`.

---

### M-017 — Acoustics-track interiors (second sheet)

**Source:** `acoustics_regularize_ladder` lines 1900–1928.

Always equal-log interiors (M-008). If anchors are monotone up or down, clip p/mp/f into the measured segment. **Does not use PCHIP.**

**Rule IDs** R0–R7 are bibliographic labels exported on `Acoustics_prior_rules`, not extra equations.

---

### M-018 — Acoustics-track outer shrink (second sheet)

**Source:** lines 1948–2022.

Local steps as in M-009 without corpus $w$. Positive teacher steps: register median of notes with $y_{\mathrm{pp}}\le y_{\mathrm{mf}}\le y_{\mathrm{ff}}$ and both log-steps $>0$ (`_rising_teacher_outer_steps`, 1839–1879).

$$\mathrm{shrink}=\begin{cases}0.35 & \text{monotone up}\\ 0.75 & \text{otherwise}\end{cases}$$

Rising: mix local step toward $\max(|\mathrm{local}|,0.02)$ or teacher. Falling: continue descent but shrink magnitude. Non-monotone: outers from $\min(y_{\mathrm{pp}},y_{\mathrm{mf}})$ and $\max(y_{\mathrm{mf}},y_{\mathrm{ff}})$ with literature-positive steps.

Then apply M-010 taper unless $r=1$.

**Layman:** A second opinion that prefers “more effort → brighter” when the three measurements allow it.

**Specialist:** Soft regularizer. Anchors never overwritten (R6). Not a physical identification of bow force.

---

### M-019 — Acoustics R3 ratio soft-cap (second sheet)

**Source:** lines 2024–2048. `ACOUSTICS_RATIO_SOFT_CAP=1.40`.

Cap $y_{\mathrm{ffff}}/y_{\mathrm{ff}}$ and $y_{\mathrm{pppp}}/y_{\mathrm{pp}}$ to $[1/1.4,\,1.4]$. Milder cap for `fff`/`ppp` uses $\sqrt{1.4}$. If unused, logs “taper alone sufficed”.

**Layman:** Do not let outer guesses explode.

**Specialist:** Asymmetric (extreme outers vs near outers). Modeling guard, not a CI.

---

### M-020 — Posterior pushforward intervals (production)

**Source:** `pushforward_predictive_intervals` lines 580–655; seed at `transfer_one_note` 2152.

1. $y_{\mathrm{obs}}=(D_{\mathrm{lo}},D_{\mathrm{hi}})$ from measured anchors; hold $\log y_{\mathrm{pp}}$ fixed.
2. $\theta^{(b)}\sim\mathcal{N}(\mu_{\mathrm{post}},\Sigma_{\mathrm{post}})$, $b=1\ldots 400$ (`N_PUSHFORWARD_DRAWS`).
3. Rebuild anchors via M-034; apply M-011 (± PCHIP).
4. Percentiles at $100(\alpha/2)$ and $100(1-\alpha/2)$ with $\alpha=0.10$.
5. Overwrite anchor bands to the **measured** values.

Need $\ge \max(20,n/10)$ finite draws per level; else NaN.

**Layman:** Wiggle the two log-steps in a way the corpus thinks is typical, rebuild the ladder, and take a 90% band on the **imputed** levels.

**Specialist:** This is a **posterior pushforward of $\theta$**, not $y_{\mathrm{new}}\sim\mathcal{N}(\theta,\Sigma_{\mathrm{meas}})$. The measurement layer is used to **form** $\Sigma_{\mathrm{post}}$, then discarded for the ladder draws. Naming in `Run_meta` is accurate; older prose saying “predictive intervals” should be read as this procedure.

**Seed (v1.5.2.2):** `seed + _stable_note_seed_offset(note)` (M-035). `run_transfer` also adds the row index to `seed`.

**Tests:** `test_pushforward_anchors_exact`, `test_pushforward_brackets_point_ladder`, `test_interval_bounds_reproducible_across_hash_seeds`.

---

### M-021 — Acklam / Beasley–Springer–Moro normal PPF (production)

**Source:** `_norm_ppf` lines 727–779.

Project-owned rational approximation of $\Phi^{-1}(p)$. Used in LOO coverage (M-022), **not** in the Monte Carlo percentiles of M-020.

**Undefined:** $p\le 0$ or $p\ge 1$ → $\pm\infty$.

**Tests:** `test_norm_ppf_matches_scipy_on_nominal_interval` vs `scipy.stats.norm.ppf` (abs $2\times 10^{-8}$).

---

### M-022 — Interval hyperparameter calibration (production)

**Source:** `calibrate_span_hyperparameters` 449–509; `_panel_prior_predictive_coverage` 407–446; `calibrate_log_span_model` 658–724.

Grid `cov_scale` on $[0.60,1.40]$ (17 values), then `meas_ratio` on $[0.15,0.85]$ (15 values), minimizing $|\mathrm{cover}(D_{\mathrm{lo}})-(1-\alpha)|$ under a **fast approximate** LOO (leave-one-out register mean, shared $\Sigma$).

Reported coverage in `Interval_calibration` uses **full** LOO refits (`_loo_prior_predictive_coverage`) plus an mf pushforward $y_{\mathrm{mf}}=\exp(\log y_{\mathrm{pp}}+D_{\mathrm{lo}}^{\mathrm{draw}})$.

**Layman:** Inflate or shrink the span covariance so about 90% of notes’ spans fall in the prior band.

**Specialist:** The fast grid and the reported LOO are **not identical** estimators. Calibration does not change the point ladder.

---

### M-023 — Held-anchor predictors (diagnostics)

**Source:** `predict_held_anchor` lines 2280–2354.

| Method | hold mf | hold pp | hold ff |
|--------|---------|---------|---------|
| `linear` | $(y_{\mathrm{pp}}+y_{\mathrm{ff}})/2$ | $(y_{\mathrm{mf}}-s\,y_{\mathrm{ff}})/(1-s)$ | $y_{\mathrm{pp}}+(y_{\mathrm{mf}}-y_{\mathrm{pp}})/s$ |
| `log_midpoint` | $\sqrt{y_{\mathrm{pp}}y_{\mathrm{ff}}}$ | $y_{\mathrm{mf}}^2/y_{\mathrm{ff}}$ | $y_{\mathrm{mf}}^2/y_{\mathrm{pp}}$ |
| `production_aligned` | $\exp(\log y_{\mathrm{pp}}+s(\log y_{\mathrm{ff}}-\log y_{\mathrm{pp}}))$ | $\exp(\log y_{\mathrm{mf}}-D_{\mathrm{lo}}^{\mathrm{corpus}})$ | $\exp(\log y_{\mathrm{mf}}+D_{\mathrm{hi}}^{\mathrm{corpus}})$ |

$s=\mathrm{clip}(\mathrm{share}_{\mathrm{lo}}^{\mathrm{corpus}},0.05,0.95)$ (default $0.5$). Corpus spans reoriented to match the observed remaining-segment sign.

**Layman:** Pretend one measurement is missing and guess it three ways.

**Specialist:** Diagnostics only — they do **not** fill production `Results`. Linear pp/ff can be $\le 0$ and is then dropped.

**Tests:** `test_predict_held_mf_log_midpoint`, `test_holdout_baselines_all_present`.

---

### M-024 — Bootstrap of corpus summaries (diagnostics)

**Source:** `bootstrap_diagnostics` 2482–2545. Default $n=400$.

Resample complete rows with replacement. Record median $R_{\mathrm{ff}/\mathrm{pp}}$, median $\mathrm{share}_{\mathrm{lo}}$, and production-aligned hold-out MAE. Percentiles $2.5/50/97.5$.

Uses sample-level corpus geom (`leave_one_out_corpus=False`) for cost.

**Seed:** independent `default_rng(seed)` — not mixed with M-035.

---

### M-025 — Outer-step sensitivity (diagnostics, not a CI)

**Source:** `outer_step_sensitivity_bands` 2548–2572. $\delta=0.20$.

Recompute M-010 with $\mathrm{step}\cdot(1\pm\delta)$. Also tabulate point outers at $r\in\{0.7,0.8,0.9,1.0\}$.

**Layman:** “If the outer step were 20% wrong, here is the range.”

**Specialist:** Deterministic; explicitly `band_type=outer_step_sensitivity_not_CI`.

---

### M-026 — Provenance kinds and quality malus (production intake)

**Source:** `_anchor_kinds_from_provenance` 1824–1836; constants 200–203.

| Upstream `prov_*` | `value_kind` / `kind_*` | Quality |
|-------------------|-------------------------|---------|
| `measured` (default) | `measured_anchor` | — |
| `edge_filled` | `edge_filled_anchor` | $-0.20$ |
| `interior_fill` | `interior_filled_anchor` | $-0.10$ |

Row-level `edge_filled_anchor` truthy → all three anchors tagged `edge_filled`.

**Layman:** If an upstream filler invented a pp/mf/ff, mark it.

**Specialist:** Does not change the numerical ladder except via quality. SDA import uses `kind_*`.

**Tests:** `test_edge_filled_anchor_quality_malus_and_kinds`.

---

### M-027 — Reconstruct anchors from spans (interval helper)

**Source:** `_anchors_from_spans` 547–554.

$$y_{\mathrm{pp}}=e^{\ell_{\mathrm{pp}}},\quad
y_{\mathrm{mf}}=e^{\ell_{\mathrm{pp}}+D_{\mathrm{lo}}},\quad
y_{\mathrm{ff}}=e^{\ell_{\mathrm{pp}}+D_{\mathrm{lo}}+D_{\mathrm{hi}}}$$

Invalid / non-positive draws become NaN in M-020.

---

### M-028 — File SHA-256 provenance (export)

**Source:** `file_sha256` 824–832. Written to `Run_meta.source_sha256` when `--panel` / `--paste-file` exists.

---

### M-029 — Octave from note label (production)

**Source:** `octave_from_note` 304–306.

Trailing digits: `C4` → $4$. No match → `None` (global $\mu_g$).

**Not** MIDI. Scientific-pitch hyphenation is not parsed beyond a final integer.

---

### M-030 — LOO interior stability (diagnostics)

**Source:** `leave_one_note_out_register` 2447–2479.

Rebuild corpus geom without each note; compare $\lvert\log y_d^{\mathrm{LOO}}-\log y_d^{\mathrm{full}}\rvert$ for $d\in\{p,mp,f\}$.

Equal-log interiors do not use `_pooled_steps`, and PCHIP does not use the corpus. The implementation comment therefore expects this MAE near zero; the diagnostic remains as a stability check if corpus-dependent interiors are introduced later.

---

### M-031 — Error summaries (diagnostics)

**Source:** `_err_summary` 2368–2379.

On a vector of already-absolute errors: $n$, MAE, median AE, RMSE, 90th percentile. Used for hold-out tables.

---

### M-032 — Positivity repair on acoustics track (safeguard)

**Source:** `acoustics_regularize_ladder` 2060–2062.

If a regularized cell is non-finite or $\le 0$, replace with the data-faithful prediction (else $y_{\mathrm{mf}}$).

---

### M-033 — Transfer skip on invalid anchors (production)

**Source:** `transfer_one_note` 2084–2098.

If any anchor is `None` or $\le 0$: status `skipped_target`, empty NaN ladder, quality $0$.

`run_transfer` uses `skipped_incomplete` when any `tgt_*` is NA.

**Tests:** `test_nonpositive_anchors_skipped`, `test_missing_anchor_rows_skipped_in_run_transfer`.

---

### M-034 — Linear-space export of logs (production)

Every stored dynamic on `Results` is $y=\exp(\ell)$ except tanh N/A cells which stay blank/NaN. No rounding beyond IEEE float / Excel.

---

### M-035 — Process-stable interval seed offset (production, v1.5.2.2)

**Source:** `_stable_note_seed_offset` 835–842; use at 2152.

```python
    digest = hashlib.sha256(str(note).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % int(modulus)
```

$$\mathrm{offset}=\bigl(\mathrm{int}_{64}(\mathrm{SHA256}(\mathrm{note})[:8])\bigr)\bmod 10007$$

**Layman:** The same note always gets the same extra random seed, on any computer.

**Specialist:** Replaces salted `hash(note)`, which moved `pred_lo`/`pred_hi` when `PYTHONHASHSEED` changed (demonstrated defect). Point estimates unchanged.

**Tests:** `test_stable_note_offset_independent_of_pythonhashseed`, `test_interval_bounds_reproducible_across_hash_seeds`.

---

### M-036 — Paste / Excel numeric parse (production)

**Source:** `_parse_number` 794–802; `_safe_float` 782–791.

Comma-as-decimal if no `.`; otherwise commas stripped as thousands separators. Require finite $v>0$.

**Layman:** `8,5` means $8.5$ in European paste.

**Specialist:** `1.234,56` is **not** supported (both separators).

**Tests:** `test_paste_parser_header_and_no_header`, `test_paste_rejects_nonpositive`.

---

### M-037 — Batch Media reconstruction (offline helper)

**Source:** `run_all_instruments_batch.py` `reconstruct_media_from_dyn_sheets` / `column_map_for_media`.

Maps local Zenodo-style workbooks onto `note,pp,mf,ff` then calls `run_pipeline`. **No new ladder law.** Hard-coded Desktop roots. Not a portable API.

`run_para_dinamicas_folder.py` likewise wraps M-011 via `run_pipeline` for a Desktop folder.

---

## Library operations

Do not treat these as project-derived formulae.

### L-001 — `numpy.log` / `numpy.exp`

**Call:** throughout `transfer.py` (e.g. M-002, M-034). Natural log. Version context: numpy 2.2.6.

**Layman:** Convert to/from log space.

**Specialist:** Domain $y>0$ enforced by project filters before the call.

### L-002 — `numpy.cov`

**Call:** `fit_log_span_model` 359, 378. `rowvar=False`, `ddof=1`. Project then applies M-004.

### L-003 — `numpy.linalg.eigh` / `inv`

**Call:** `_psd` 333; `posterior` 275–278. Library eigensolver / inverse; ridge is project math.

### L-004 — `numpy.random.Generator.multivariate_normal`

**Call:** `pushforward_predictive_intervals` 611. `size=n_draws`. Surrounding model is M-020.

### L-005 — `numpy.percentile` / `quantile`

**Call:** interval bands 636–637; calibration mf 703; bootstrap 2525–2527. Linear interpolation (NumPy default).

### L-006 — `scipy.interpolate.PchipInterpolator`

**Call:** M-015. `extrapolate=False`. Version: scipy 1.15.3. Fritsch–Carlson internals **not** reproduced.

### L-007 — `pandas.read_excel`

**Call:** `_attach_panel_provenance` 1303. Provenance merge only.

### L-008 — `openpyxl.load_workbook`

**Call:** `_read_workbook_rows` 1017. `data_only=True`, `read_only=True`.

### L-009 — `pandas.ExcelWriter` (`openpyxl` engine)

**Call:** `export_excel` 3255. Workbook assembly; no extra maths.

### L-010 — `numpy.median`

**Call:** corpus summaries 1369–1373; teacher steps 1861–1868. Aggregation only.

### L-011 — `hashlib.sha256`

**Call:** M-028, M-035. Cryptographic hash used as a stable integer, not as a scientific transform.

NumPy arithmetic inside M-008–M-012 remains **project** math (not L-IDs).

---

## Entry points (actual)

| Path | Function | Notes |
|------|----------|-------|
| `python -m dynamics_predicter` / `dynamics-predicter` | `transfer.main` 3340–3387 | `--panel`, `--paste-file`, `--out`, `--pchip`, `--n-boot`, `--seed` |
| `dynamics-predicter-gui` / `run_gui.py` / `start.bat` | `gui.main` → `App` | Excel mapper + paste; `_export` → `run_pipeline` |
| `dynamic_shape_transfer.py` | shim to `transfer.main` | Legacy |
| `run_all_instruments_batch.py` | local batch | Desktop corpus; optional |
| `run_para_dinamicas_folder.py` | local batch | Desktop corpus; optional |

No notebooks. GUI `_export` (289–315) is the real Run path (`run_pipeline` + `outer_sensitivity_table` + `export_excel`).

---

## Omissions

- Library internals of NumPy / SciPy / openpyxl solvers.
- Tk geometry.
- Historical research workbooks and `outputs/compare_tracks.json` generation (committed snapshot; not regenerated here).
- Closed-form solution of the tanh Brent search (iterative; M-012/M-014).
- StackEdit browser rendering was **not** opened. Delimiters were checked in the text (inline `$...$`, display `$$...$$`, no equations inside code fences).

---

## Scientific ambiguities (unresolved intent — not silently “fixed”)

1. **Index spacing vs musical dynamics.** Equal $\mathrm{idx}$ steps are a convention (M-001). Notated *p / mp / mf / f* are not shown to be equal in the recorded metric.
2. **Recorded metric $\neq$ SPL.** Code never converts to decibels. CDM/EWSD can fall as markings rise; that is treated as data, not a bug.
3. **Interval semantics.** Posterior of $\theta$ pushforward (M-020) vs a full posterior-predictive $y_{\mathrm{new}}$. Implementation matches `INTERVAL_KIND`; it is not lab-error.
4. **Acoustics track vs PCHIP.** Second sheet always equal-log interiors even when GUI PCHIP is on.
5. **Calibration grid vs reported LOO** (M-022) can disagree on coverage.
6. **Duplicates.** `validate_panel_df` reports them; load/paste do not hard-fail on duplicates (paste treats them as non-hard).
7. **CLI default panel path** is a specific Desktop folder — not portable.
8. **Batch scripts** hard-code other Desktop trees; they are local operators, not the reviewed public contract.

No new extrapolation law was imposed. Monotonicity was not forced on measured anchors.

---

## References actually consulted

- First-party sources listed in the hash table (this working tree).
- `README.md`, `CHANGES.md`, `LITERATURE.md`, `tests/*.py`.
- Isolated-environment `pip freeze` (2026-09-17 review).
- Synthetic CLI/GUI exports under `Dynamics_extrapol_review_20260917_161823/exports/` (external).
- SciPy `norm.ppf` used only as an external check of M-021.

No journal PDFs were opened for this extraction; literature **titles** follow `LITERATURE.md` / `ACOUSTICS_LITERATURE_RULES` as cited by the code.
