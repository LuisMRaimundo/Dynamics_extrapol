# Literature titles (method grounding)

**Scope:** IOWA+ORCHIDEA-only equal-log imputation with tapered outers (R7), optional tanh-saturating third track, aligned hold-outs, conjugate EB posterior pushforward intervals on log-spans (calibrated `meas_ratio` / `cov_scale`), bootstrap diagnostics, and optional PCHIP (Philharmonia not used). All curvature nuance stays in the same log space — no extra transform layer.

| Role in method | Title | Authors | Year |
|----------------|-------|---------|------|
| Missing-data / structural gaps | **Statistical Analysis with Missing Data** (3rd ed.) | Little & Rubin | 2019 |
| Log / multilevel / partial pooling | **Data Analysis Using Regression and Multilevel/Hierarchical Models** | Gelman & Hill | 2007 |
| Ratio / relative structure | **Sampling Techniques** (3rd ed.) | Cochran | 1977 |
| Shape-preserving interpolation | **Monotone Piecewise Cubic Interpolation** | Fritsch & Carlson | 1980 |
| Imputation diagnostics | **Flexible Imputation of Missing Data** (2nd ed.) | van Buuren | 2018 |
| Uncertainty / resampling | **An Introduction to the Bootstrap** | Efron & Tibshirani | 1993 |

## Acoustics prior (second track: `Results_acoustics_prior`)

Primary **strings-only** book (local library):

| Role | Title | Authors / editor | Local path |
|------|-------|------------------|------------|
| **PRIMARY strings framework** | **The Science of String Instruments** | Thomas D. Rossing (ed.) | `E:\Bibliografia geral\Acustica\Thomas D. Rossing_The Science of String Instruments.pdf` |

Supporting acoustics (same folder / `Strings\`):

| Role | Title | Notes |
|------|-------|-------|
| General musical acoustics | Fundamentals of Musical Acoustics | Benade |
| Instrument acoustics | Acoustics of Musical Instruments | Chaigne & Kergomard (2016) |
| Performance / ensemble | Acoustics and the Performance of Music | Jürgen Meyer |
| Violin research survey | A history of violin research | Schelleng / Cremer lineage context |
| Absolute levels | Absolute Amplitudes and Spectra… | Spectrum ≠ free SPL scaling |

Rules R0–R7 are exported on sheet `Acoustics_prior_rules`. Measured `pp`/`mf`/`ff` are never overwritten (R6). R7 cites Meyer (2009) dynamic-range compression and Patterson (1974) for outer taper `step·r^(k−1)`.

**EWSD / CDM vs dynamics:** the spectral-density score is empirically non-monotone
along the dynamic ladder for bowed strings (cello corpus: 33/49 notes with
\(\mathrm{ff}<\mathrm{mf}\); median \(R_{\mathrm{ff}/\mathrm{mf}}\approx 0.94\)).
Do not treat soft→loud CDM increase as a physical law; ladder tests must scope
hygiene to imputed/extrapolated cells only (`value_kind` / `kind_*` on `Results`).
