#!/usr/bin/env python3
"""
IOWA+ORCHIDEA-only dynamic imputation (research-grade pipeline).

Build a 10-level ladder (pppp…ffff) from measured anchors pp, mf, ff.

Primary estimator: equal-log placement inside segments + segment-step outers.
Optional: PCHIP polish on the three anchors only (Fritsch & Carlson), then
evaluate at intermediate dynamic indices; outers stay equal-log/step with
optional geometric taper (v1.5). Third track: tanh_saturating in log space.

No Philharmonia.

Scientific framing (see LITERATURE.md / README.md):
  - Structural missingness (Little & Rubin; van Buuren)
  - Log / ratio geometry on measured anchors (Cochran; Gelman & Hill)
  - Shape-preserving interpolation (Fritsch & Carlson PCHIP)
  - Hold-out diagnostics aligned with the production estimator (van Buuren)
  - Model-based predictive intervals: bivariate normal on log-spans (D_lo, D_hi)
    with register partial pooling (Gelman & Hill), Monte Carlo pushforward through
    the equal-log ladder; outer-step ±20% kept as a separate sensitivity analysis
  - Outer taper + saturating tanh track (Meyer 2009; Patterson 1974) — curvature
    nuance in the same log space (no extra transform layer)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from scipy.interpolate import PchipInterpolator

__version__ = "1.5.2.2"

DYN_ORDER = ("pppp", "ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "ffff")
DYN_LEVEL = {d: float(i) for i, d in enumerate(DYN_ORDER)}
ANCHORS = ("pp", "mf", "ff")
INTERPOLATED = ("p", "mp", "f")
EXTRAPOLATED_BEYOND = ("pppp", "ppp", "fff", "ffff")

UNIFORM_FRAC_LO = {"p": 1.0 / 3.0, "mp": 2.0 / 3.0}
UNIFORM_FRAC_HI = {"f": 0.5}
N_STEPS_LO = 3  # pp → p → mp → mf
N_STEPS_HI = 2  # mf → f → ff
OUTER_SENSITIVITY = 0.20  # ±20% outer step size (sensitivity only, not CI)
# Geometric taper on successive outer log-steps: offset_k = step * r^(k-1)
# r=1.0 reproduces v1.4 bit-exactly; default 0.80 (Meyer dynamic-range compression).
OUTER_TAPER_R = 0.80
OUTER_TAPER_SENSITIVITY_RS = (0.7, 0.8, 0.9, 1.0)
POOL_EPS = 1e-6  # shrink tiny segment steps toward corpus
BOOTSTRAP_DEFAULT = 400
RNG_SEED_DEFAULT = 20260801
PRED_INTERVAL_ALPHA = 0.10  # 90% predictive intervals
N_PUSHFORWARD_DRAWS = 400
# Prior strength for register-mean pooling (Gelman & Hill–style EB)
SPAN_SHRINK_N0 = 0.35
# y|θ ~ N(θ, meas_ratio · Σ_within); ratio auto-calibrated unless overridden
DEFAULT_MEAS_RATIO = 0.35
COV_RIDGE = 1e-6
MIN_MODEL_N = 5
INTERVAL_KIND = "eb_gaussian_posterior_pushforward"
LADDER_MODE_EQUAL_LOG = "equal_log"
LADDER_MODE_TANH = "tanh_saturating"

# Acoustics-prior hyperparameters (literature-conditioned soft regularizer)
ACOUSTICS_OUTER_SHRINK_MONO = 0.35  # blend toward positive corpus step when anchors monotone
ACOUSTICS_OUTER_SHRINK_NONMONO = 0.75  # stronger shrink when anchors violate force→brightness
ACOUSTICS_MIN_POS_STEP = 0.02  # minimum positive log-step when forcing literature direction on outers
ACOUSTICS_RATIO_SOFT_CAP = 1.40  # Meyer: spectrum change << typical SPL spans; soft-cap outer growth

# Explicit literature → rule table (exported on Acoustics_prior_rules sheet)
# Local bibliography root (user library). Citations remain valid without these files.
ACOUSTICS_BIBLIO_ROOT = r"E:\Bibliografia geral\Acustica"

# Primary strings-only reference in the local library (Rossing, ed.).
ACOUSTICS_STRINGS_BOOK = rf"{ACOUSTICS_BIBLIO_ROOT}\Thomas D. Rossing_The Science of String Instruments.pdf"
ACOUSTICS_STRINGS_BOOK_ALT = rf"{ACOUSTICS_BIBLIO_ROOT}\The Science of String Instruments.pdf"

ACOUSTICS_LITERATURE_RULES: list[dict[str, str]] = [
    {
        "rule_id": "R0_string_instrument_framework",
        "authors_year": "Rossing (ed., 2010); Benade; Chaigne & Kergomard (2016)",
        "work": "The Science of String Instruments (PRIMARY strings-only book); Fundamentals of Musical Acoustics; Acoustics of Musical Instruments",
        "claim": "Bowed-string radiated spectrum is shaped by Helmholtz motion, string/body coupling, and player controls — dynamics are not a free multiplicative SPL knob on a fixed spectrum.",
        "implementation": "Second-track prior only; never overwrite measured anchors; regularize imputed/extrapolated cells with modest spectral-enrichment bias, not SPL decades. Ground string physics in Rossing before general acoustics texts.",
        "local_path": rf"{ACOUSTICS_STRINGS_BOOK} ; {ACOUSTICS_STRINGS_BOOK_ALT} ; {ACOUSTICS_BIBLIO_ROOT}\Strings\The Science of String Instruments.pdf ; {ACOUSTICS_BIBLIO_ROOT}\Fundamentals of Musical Acoustics -- Benade.pdf ; {ACOUSTICS_BIBLIO_ROOT}\chaigne_2016_Acoustics of Musical.pdf",
    },
    {
        "rule_id": "R1_force_brightness_direction",
        "authors_year": "Schoonderwaldt (2009); Askenfelt / Guettler; Rossing (ed.) bowed chapters",
        "work": "Mechanics and Acoustics of Violin Bowing; The Science of String Instruments (bowed-string chapters)",
        "claim": "Bow force is the dominant controller of spectral centroid / brightness; higher effort typically enriches the spectrum.",
        "implementation": "Prefer non-decreasing ladders when measured anchors allow; place imputed p/mp/f on a monotone path between fixed anchors; for outers, prefer continuing a positive enrichment step.",
        "local_path": rf"{ACOUSTICS_STRINGS_BOOK} ; {ACOUSTICS_BIBLIO_ROOT}\A history of violin research.pdf ; {ACOUSTICS_BIBLIO_ROOT}\Strings\A history of violin research.pdf",
    },
    {
        "rule_id": "R2_schelleng_control_region",
        "authors_year": "Schelleng (1973); Cremer; Rossing (ed.); Chaigne & Kergomard",
        "work": "The bowed string and the player; Schelleng / Helmholtz control region in The Science of String Instruments",
        "claim": "Playable dynamics live in a constrained force–β Helmholtz region; inconsistent control should not be extrapolated as free loudness.",
        "implementation": "When anchors are non-monotonic (inconsistent with a single force slider), shrink outer pppp/ppp/fff/ffff steps strongly toward a corpus positive step instead of amplifying local anomalies.",
        "local_path": rf"{ACOUSTICS_STRINGS_BOOK} ; {ACOUSTICS_BIBLIO_ROOT}\Strings\The Science of String Instruments.pdf ; {ACOUSTICS_BIBLIO_ROOT}\chaigne_2016_Acoustics of Musical.pdf",
    },
    {
        "rule_id": "R3_spectrum_not_spl",
        "authors_year": "Meyer (2009); Sivian et al. (absolute amplitudes); Perez et al. (ICA 2007)",
        "work": "Acoustics and the Performance of Music; Absolute Amplitudes and Spectra…",
        "claim": "Dynamic marking changes the spectrum, but spectral descriptors do not track SPL one-to-one; gains are often modest and frequency-dependent.",
        "implementation": "Soft-cap aggressive outer growth (ACOUSTICS_RATIO_SOFT_CAP); never invent SPL-like decades of change in the EWSD-like metric.",
        "local_path": rf"{ACOUSTICS_BIBLIO_ROOT}\IMP_Acoustics and the Performance of music_(Jürgen Meyer) (z-lib.org).pdf ; {ACOUSTICS_BIBLIO_ROOT}\ABSOLUTE AMPLITUDES AND SPECTRA OF CERTAIN MUSICAL INSTRUMENTS AND ORCHESTRAS.pdf ; {ACOUSTICS_BIBLIO_ROOT}\Benade_Spectral similarities of tones from especially useful.pdf",
    },
    {
        "rule_id": "R4_helmholtz_corner_enrichment",
        "authors_year": "Cremer; Guettler & Askenfelt; Rossing (ed.); Benade",
        "work": "Helmholtz-corner / bow-force chapters in The Science of String Instruments; Fundamentals of Musical Acoustics",
        "claim": "Higher relative bow force sharpens the Helmholtz corner and strengthens high harmonics.",
        "implementation": "Directional prior on outer steps: soft side continues downward in log-metric only when the adjacent measured segment is truly falling; otherwise bias outers toward enrichment (rising) consistent with force increase.",
        "local_path": rf"{ACOUSTICS_STRINGS_BOOK} ; {ACOUSTICS_BIBLIO_ROOT}\Fundamentals of Musical Acoustics -- Benade.pdf ; {ACOUSTICS_BIBLIO_ROOT}\A history of violin research.pdf",
    },
    {
        "rule_id": "R5_register_modulation",
        "authors_year": "Rossing (ed.); Schoonderwaldt; Meyer; Chaigne & Kergomard",
        "work": "Register / radiation chapters in The Science of String Instruments; Meyer performance acoustics",
        "claim": "Available force, radiation, and spectral envelope change with register; one global dynamic slider is unrealistic.",
        "implementation": "Pool outer steps toward register-specific medians computed from notes whose anchors are literature-compatible (strictly rising pp→mf→ff).",
        "local_path": rf"{ACOUSTICS_STRINGS_BOOK} ; {ACOUSTICS_BIBLIO_ROOT}\IMP_Acoustics and the Performance of music_(Jürgen Meyer) (z-lib.org).pdf ; {ACOUSTICS_BIBLIO_ROOT}\chaigne_2016_Acoustics of Musical.pdf",
    },
    {
        "rule_id": "R6_anchors_inviolable",
        "authors_year": "Little & Rubin; van Buuren (methodological)",
        "work": "Missing-data / imputation principles",
        "claim": "Measured values must not be overwritten by a model prior.",
        "implementation": "pp, mf, ff remain exactly the IOWA+ORCHIDEA measurements on Results_acoustics_prior; only imputed/extrapolated cells are regularized.",
        "local_path": "",
    },
    {
        "rule_id": "R7_outer_taper_compression",
        "authors_year": "Meyer (2009); Patterson (1974); Patterson & Green (related auditory compression)",
        "work": "Acoustics and the Performance of Music (dynamic-range compression); auditory intensity coding / compressive transforms",
        "claim": "Perceived and performance dynamic range is compressive: equal notated steps farther from the playable center contribute diminishing spectral/effort increments — not a second free SPL decade.",
        "implementation": (
            "Outer log-steps use geometric taper step·r^(k−1) with OUTER_TAPER_R=0.80 (internal_default); "
            "r=1.0 reproduces v1.4 equal steps bit-exactly. R3 soft-cap remains a final guard; "
            "when the taper alone keeps outers inside the cap, that is logged. Same log space — no new transform layer."
        ),
        "local_path": rf"{ACOUSTICS_BIBLIO_ROOT}\IMP_Acoustics and the Performance of music_(Jürgen Meyer) (z-lib.org).pdf",
    },
]

# Fallback layout for Violino_dinámicas.xlsx (0-based)
NOTE_COL = 0
TARGET_NOTE_COL = 8
TARGET_COLS = {"pp": 9, "mf": 10, "ff": 11}

HEADER_ALIASES = {
    "note": "note",
    "notes": "note",
    "nota": "note",
    "notas": "note",
    "pitch": "note",
    "note_target": "note_target",
    "target_note": "note_target",
    "iowa_note": "note_target",
    "pp": "pp",
    "pianissimo": "pp",
    "tgt_pp": "pp",
    "iowa_pp": "pp",
    "orchidea_pp": "pp",
    "mf": "mf",
    "mezzo-forte": "mf",
    "mezzo_forte": "mf",
    "tgt_mf": "mf",
    "iowa_mf": "mf",
    "orchidea_mf": "mf",
    "ff": "ff",
    "fortissimo": "ff",
    "tgt_ff": "ff",
    "iowa_ff": "ff",
    "orchidea_ff": "ff",
    "prov_pp": "prov_pp",
    "prov_mf": "prov_mf",
    "prov_ff": "prov_ff",
    "edge_filled_anchor": "edge_filled_anchor",
}

# Quality malus when panel anchors were filled upstream (extrapol_data --fill-panel)
EDGE_FILLED_QUALITY_MALUS = 0.20
INTERIOR_FILLED_QUALITY_MALUS = 0.10
EDGE_FILLED_FLAG = "edge_filled_anchor"
INTERIOR_FILLED_FLAG = "interior_filled_anchor"


@dataclass
class NoteResult:
    note: str
    note_target: str
    target_measured: dict[str, float]
    target_pred: dict[str, float]
    pred_lo: dict[str, float]
    pred_hi: dict[str, float]
    value_kind: dict[str, str]
    status: str
    warnings: list[str]
    quality: float
    flags: list[str]
    audit: dict[str, float] = field(default_factory=dict)
    interval_kind: str = ""
    # Literature-conditioned second track (optional sheet)
    target_pred_acoustics: dict[str, float] = field(default_factory=dict)
    acoustics_rules_applied: list[str] = field(default_factory=list)
    acoustics_notes: list[str] = field(default_factory=list)
    # Third track: tanh_saturating in log space (never the default Results sheet)
    target_pred_tanh: dict[str, float] = field(default_factory=dict)
    tanh_fit: dict[str, float] = field(default_factory=dict)


@dataclass
class LogSpanModel:
    """
    Hierarchical Gaussian model on spans θ = (D_lo, D_hi):

      θ_i | r_i  ~ N(μ_{r_i}, Σ_within)
      y_i | θ_i  ~ N(θ_i, Σ_meas),   Σ_meas = meas_ratio · Σ_within

    Register means μ_r are partially pooled toward μ_global.
    Note-level intervals use the conjugate Gaussian posterior of θ | y.
    """

    mu_global: np.ndarray  # shape (2,)
    cov_within: np.ndarray  # shape (2, 2) — between-note residual within registers
    cov_marginal: np.ndarray  # pooled residual cov (fallback / predictive)
    register_mu: dict[int, np.ndarray]  # octave -> (2,)
    register_n: dict[int, int]
    shrink_n0: float
    meas_ratio: float
    tau2_between: float  # mean between-register variance of register means
    n: int
    cov_scale: float = 1.0  # LOO-calibrated inflation of predictive cov
    method: str = "eb_gaussian_hierarchical_log_spans"

    @property
    def cov(self) -> np.ndarray:
        """Backward-compatible alias: scaled within cov used as prior for θ."""
        return self.cov_scale * self.cov_within

    @property
    def meas_cov(self) -> np.ndarray:
        return float(self.meas_ratio) * self.cov

    def mu_for(self, octave: int | None) -> np.ndarray:
        if octave is not None and octave in self.register_mu:
            return np.asarray(self.register_mu[octave], dtype=float)
        return np.asarray(self.mu_global, dtype=float)

    def posterior(self, y_obs: np.ndarray, octave: int | None) -> tuple[np.ndarray, np.ndarray]:
        """Conjugate Gaussian posterior θ | y for one note."""
        mu0 = self.mu_for(octave)
        s0 = self.cov
        sm = self.meas_cov
        # Σ_post = (Σ0^{-1} + Σm^{-1})^{-1}
        # μ_post = Σ_post (Σ0^{-1} μ0 + Σm^{-1} y)
        prec0 = np.linalg.inv(s0)
        precm = np.linalg.inv(sm)
        prec_post = prec0 + precm
        cov_post = np.linalg.inv(prec_post)
        mu_post = cov_post @ (prec0 @ mu0 + precm @ np.asarray(y_obs, dtype=float))
        cov_post = 0.5 * (cov_post + cov_post.T)
        return mu_post, cov_post

    def summary(self) -> dict[str, Any]:
        c = self.cov
        return {
            "method": self.method,
            "n": self.n,
            "mu_D_lo": float(self.mu_global[0]),
            "mu_D_hi": float(self.mu_global[1]),
            "var_D_lo_within": float(self.cov_within[0, 0]),
            "var_D_hi_within": float(self.cov_within[1, 1]),
            "cov_lo_hi_within": float(self.cov_within[0, 1]),
            "corr_lo_hi": float(
                c[0, 1] / np.sqrt(c[0, 0] * c[1, 1]) if c[0, 0] > 0 and c[1, 1] > 0 else 0.0
            ),
            "tau2_between_registers": float(self.tau2_between),
            "meas_ratio": float(self.meas_ratio),
            "cov_scale": float(self.cov_scale),
            "registers": {str(k): int(v) for k, v in sorted(self.register_n.items())},
            "shrink_n0": self.shrink_n0,
        }


def octave_from_note(note: str) -> int | None:
    m = re.search(r"(\d+)\s*$", str(note).strip())
    return int(m.group(1)) if m else None


def _complete_anchor_rows(df: pd.DataFrame, exclude_idx: Iterable[Any] | None = None) -> pd.DataFrame:
    exclude = set(exclude_idx or [])
    rows = []
    for idx, r in df.iterrows():
        if idx in exclude:
            continue
        if any(pd.isna(r.get(f"tgt_{a}")) for a in ANCHORS):
            continue
        pp, mf, ff = float(r["tgt_pp"]), float(r["tgt_mf"]), float(r["tgt_ff"])
        if min(pp, mf, ff) <= 0:
            continue
        note = str(r.get("note_target") or r.get("note") or "")
        d_lo = float(np.log(mf) - np.log(pp))
        d_hi = float(np.log(ff) - np.log(mf))
        rows.append({"idx": idx, "note": note, "octave": octave_from_note(note), "D_lo": d_lo, "D_hi": d_hi})
    return pd.DataFrame(rows)


def _psd(cov: np.ndarray) -> np.ndarray:
    c = np.asarray(cov, dtype=float)
    if c.ndim == 0:
        c = np.array([[float(c), 0.0], [0.0, float(c)]], dtype=float)
    c = 0.5 * (c + c.T) + np.eye(2) * COV_RIDGE
    # eigenvalue floor
    w, v = np.linalg.eigh(c)
    w = np.maximum(w, COV_RIDGE)
    return (v * w) @ v.T


def fit_log_span_model(
    df: pd.DataFrame,
    *,
    exclude_idx: Iterable[Any] | None = None,
    shrink_n0: float = SPAN_SHRINK_N0,
    meas_ratio: float | None = None,
    cov_scale: float = 1.0,
    calibrate: bool = False,
    alpha: float = PRED_INTERVAL_ALPHA,
) -> LogSpanModel | None:
    """
    Fit hierarchical Gaussian model on (D_lo, D_hi).

    If calibrate=True, choose meas_ratio and cov_scale by LOO so prior-predictive
    coverage of D_lo is near the nominal 1-α (tighter probabilistic calibration).
    """
    panel = _complete_anchor_rows(df, exclude_idx=exclude_idx)
    if len(panel) < MIN_MODEL_N:
        return None
    X = panel[["D_lo", "D_hi"]].to_numpy(dtype=float)
    mu_g = np.mean(X, axis=0)
    cov_marg = _psd(np.cov(X, rowvar=False, ddof=1))

    reg_mu: dict[int, np.ndarray] = {}
    reg_n: dict[int, int] = {}
    raw_means: list[np.ndarray] = []
    resid_rows: list[np.ndarray] = []
    for octv, g in panel.dropna(subset=["octave"]).groupby("octave"):
        oct_i = int(octv)
        n_r = int(len(g))
        m_r = g[["D_lo", "D_hi"]].to_numpy(dtype=float).mean(axis=0)
        pooled = (n_r * m_r + shrink_n0 * mu_g) / (n_r + shrink_n0)
        reg_mu[oct_i] = pooled
        reg_n[oct_i] = n_r
        raw_means.append(m_r)
        for _, row in g.iterrows():
            resid_rows.append(np.array([row["D_lo"], row["D_hi"]], dtype=float) - pooled)

    if resid_rows:
        R = np.vstack(resid_rows)
        cov_within = _psd(np.cov(R, rowvar=False, ddof=1) if len(R) > 2 else cov_marg)
    else:
        cov_within = cov_marg.copy()

    if len(raw_means) >= 2:
        M = np.vstack(raw_means)
        tau2 = float(np.mean(np.var(M, axis=0, ddof=1)))
    else:
        tau2 = 0.0

    ratio = float(DEFAULT_MEAS_RATIO if meas_ratio is None else meas_ratio)
    scale = float(cov_scale)
    model = LogSpanModel(
        mu_global=mu_g,
        cov_within=cov_within,
        cov_marginal=cov_marg,
        register_mu=reg_mu,
        register_n=reg_n,
        shrink_n0=float(shrink_n0),
        meas_ratio=ratio,
        tau2_between=tau2,
        n=int(len(panel)),
        cov_scale=scale,
    )
    if calibrate and exclude_idx is None:
        model = calibrate_span_hyperparameters(df, model, alpha=alpha)
    return model


def _panel_prior_predictive_coverage(
    panel: pd.DataFrame,
    *,
    register_mu: dict[int, np.ndarray],
    mu_global: np.ndarray,
    cov_within: np.ndarray,
    meas_ratio: float,
    cov_scale: float,
    alpha: float,
) -> dict[str, float]:
    """
    Fast approximate LOO coverage: leave-one-out register mean, shared Σ.
    Used for hyperparameter search; final report still uses full LOO fits.
    """
    q = (_norm_ppf(alpha / 2.0), _norm_ppf(1.0 - alpha / 2.0))
    pred_cov = cov_scale * cov_within * (1.0 + float(meas_ratio))
    sd = np.sqrt(np.diag(pred_cov))
    cover_lo = cover_hi = n = 0
    for octv, g in panel.groupby("octave"):
        if pd.isna(octv):
            mu = mu_global
            for _, row in g.iterrows():
                d_lo, d_hi = float(row["D_lo"]), float(row["D_hi"])
                cover_lo += int(mu[0] + q[0] * sd[0] <= d_lo <= mu[0] + q[1] * sd[0])
                cover_hi += int(mu[1] + q[0] * sd[1] <= d_hi <= mu[1] + q[1] * sd[1])
                n += 1
            continue
        arr = g[["D_lo", "D_hi"]].to_numpy(dtype=float)
        for i in range(len(arr)):
            if len(arr) == 1:
                mu = register_mu.get(int(octv), mu_global)
            else:
                mu = arr[np.arange(len(arr)) != i].mean(axis=0)
            d_lo, d_hi = float(arr[i, 0]), float(arr[i, 1])
            cover_lo += int(mu[0] + q[0] * sd[0] <= d_lo <= mu[0] + q[1] * sd[0])
            cover_hi += int(mu[1] + q[0] * sd[1] <= d_hi <= mu[1] + q[1] * sd[1])
            n += 1
    if n == 0:
        return {}
    return {"coverage_D_lo": cover_lo / n, "coverage_D_hi": cover_hi / n, "n": float(n)}


def calibrate_span_hyperparameters(
    df: pd.DataFrame,
    base: LogSpanModel,
    *,
    alpha: float = PRED_INTERVAL_ALPHA,
    target_coverage: float | None = None,
) -> LogSpanModel:
    """
    Choose cov_scale and meas_ratio so approximate LOO prior-predictive coverage
    of D_lo matches the nominal level.
    """
    target = float(1.0 - alpha if target_coverage is None else target_coverage)
    panel = _complete_anchor_rows(df)
    best_scale, best_err = 1.0, 1e9
    for scale in np.linspace(0.60, 1.40, 17):
        cal = _panel_prior_predictive_coverage(
            panel,
            register_mu=base.register_mu,
            mu_global=base.mu_global,
            cov_within=base.cov_within,
            meas_ratio=base.meas_ratio,
            cov_scale=float(scale),
            alpha=alpha,
        )
        cov_lo = cal.get("coverage_D_lo", float("nan"))
        if not np.isfinite(cov_lo):
            continue
        err = abs(cov_lo - target)
        if err < best_err:
            best_err, best_scale = err, float(scale)

    best_ratio, best_err_r = base.meas_ratio, 1e9
    for ratio in np.linspace(0.15, 0.85, 15):
        cal = _panel_prior_predictive_coverage(
            panel,
            register_mu=base.register_mu,
            mu_global=base.mu_global,
            cov_within=base.cov_within,
            meas_ratio=float(ratio),
            cov_scale=best_scale,
            alpha=alpha,
        )
        cov_lo = cal.get("coverage_D_lo", float("nan"))
        if not np.isfinite(cov_lo):
            continue
        err = abs(cov_lo - target)
        if err < best_err_r:
            best_err_r, best_ratio = err, float(ratio)

    return LogSpanModel(
        mu_global=base.mu_global,
        cov_within=base.cov_within,
        cov_marginal=base.cov_marginal,
        register_mu=base.register_mu,
        register_n=base.register_n,
        shrink_n0=base.shrink_n0,
        meas_ratio=best_ratio,
        tau2_between=base.tau2_between,
        n=base.n,
        cov_scale=best_scale,
    )


def _loo_prior_predictive_coverage(
    df: pd.DataFrame,
    template: LogSpanModel,
    *,
    alpha: float,
) -> dict[str, float]:
    """Coverage of observed spans under full LOO N(μ_r, Σ_prior + Σ_meas)."""
    complete = df.dropna(subset=[f"tgt_{a}" for a in ANCHORS])
    q = (_norm_ppf(alpha / 2.0), _norm_ppf(1.0 - alpha / 2.0))
    cover_lo = cover_hi = n = 0
    for idx, r in complete.iterrows():
        model = fit_log_span_model(
            complete,
            exclude_idx=[idx],
            shrink_n0=template.shrink_n0,
            meas_ratio=template.meas_ratio,
            cov_scale=template.cov_scale,
            calibrate=False,
        )
        if model is None:
            continue
        note = str(r.get("note_target") or r.get("note") or "")
        mu = model.mu_for(octave_from_note(note))
        pred_cov = model.cov + model.meas_cov
        sd = np.sqrt(np.diag(pred_cov))
        d_lo = float(np.log(r["tgt_mf"]) - np.log(r["tgt_pp"]))
        d_hi = float(np.log(r["tgt_ff"]) - np.log(r["tgt_mf"]))
        cover_lo += int(mu[0] + q[0] * sd[0] <= d_lo <= mu[0] + q[1] * sd[0])
        cover_hi += int(mu[1] + q[0] * sd[1] <= d_hi <= mu[1] + q[1] * sd[1])
        n += 1
    if n == 0:
        return {}
    return {"coverage_D_lo": cover_lo / n, "coverage_D_hi": cover_hi / n, "n": float(n)}


def _anchors_from_spans(log_pp: float, d_lo: float, d_hi: float) -> dict[str, float]:
    log_mf = log_pp + d_lo
    log_ff = log_mf + d_hi
    return {
        "pp": float(np.exp(log_pp)),
        "mf": float(np.exp(log_mf)),
        "ff": float(np.exp(log_ff)),
    }


def ladder_values_from_anchors(
    tgt_anchors: dict[str, float],
    *,
    use_pchip: bool = False,
    corpus_geom: dict[str, float] | None = None,
    outer_taper_r: float = OUTER_TAPER_R,
    ladder_mode: str = LADDER_MODE_EQUAL_LOG,
) -> dict[str, float]:
    """Point ladder only (no intervals). ladder_mode: equal_log (default) | tanh_saturating."""
    tgt_log = {a: float(np.log(tgt_anchors[a])) for a in ANCHORS}
    if ladder_mode == LADDER_MODE_TANH:
        pred_log, _, _ = place_tanh_saturating_ladder(tgt_log)
    else:
        pred_log, _, _ = place_equal_log_ladder(
            tgt_log, corpus_geom=corpus_geom, outer_taper_r=outer_taper_r
        )
        if use_pchip:
            pred_log.update(pchip_intermediates_from_anchors(tgt_log))
    for a in ANCHORS:
        pred_log[a] = tgt_log[a]
    return {d: float(np.exp(pred_log[d])) for d in DYN_ORDER}


def pushforward_predictive_intervals(
    tgt_anchors: dict[str, float],
    model: LogSpanModel,
    *,
    octave: int | None,
    use_pchip: bool = False,
    corpus_geom: dict[str, float] | None = None,
    n_draws: int = N_PUSHFORWARD_DRAWS,
    alpha: float = PRED_INTERVAL_ALPHA,
    seed: int = RNG_SEED_DEFAULT,
    outer_taper_r: float = OUTER_TAPER_R,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """
    (1-α) intervals via conjugate EB posterior pushforward.

    Point ladder uses measured anchors. Interval draws: θ ~ N(μ_post, Σ_post)
    where μ_post, Σ_post are the Gaussian posterior of latent spans given y_obs,
    then the equal-log ladder is reapplied (log pp held at the measured value).
    Measured anchors are reported exact (lo=hi=measured).
    """
    log_pp = float(np.log(tgt_anchors["pp"]))
    y_obs = np.array(
        [
            float(np.log(tgt_anchors["mf"]) - log_pp),
            float(np.log(tgt_anchors["ff"]) - np.log(tgt_anchors["mf"])),
        ],
        dtype=float,
    )
    mu_post, cov_post = model.posterior(y_obs, octave)

    rng = np.random.default_rng(seed)
    draws = rng.multivariate_normal(mu_post, cov_post, size=int(n_draws))
    samples = {d: np.empty(n_draws, dtype=float) for d in DYN_ORDER}
    for b in range(n_draws):
        d_lo, d_hi = float(draws[b, 0]), float(draws[b, 1])
        anchors_b = _anchors_from_spans(log_pp, d_lo, d_hi)
        if min(anchors_b.values()) <= 0 or not all(np.isfinite(v) for v in anchors_b.values()):
            for d in DYN_ORDER:
                samples[d][b] = np.nan
            continue
        ladder = ladder_values_from_anchors(
            anchors_b,
            use_pchip=use_pchip,
            corpus_geom=corpus_geom,
            outer_taper_r=outer_taper_r,
        )
        for d in DYN_ORDER:
            samples[d][b] = ladder[d]

    q_lo, q_hi = 100.0 * (alpha / 2.0), 100.0 * (1.0 - alpha / 2.0)
    pred_lo, pred_hi = {}, {}
    for d in DYN_ORDER:
        arr = samples[d][np.isfinite(samples[d])]
        if arr.size < max(20, n_draws // 10):
            pred_lo[d] = pred_hi[d] = float("nan")
        else:
            pred_lo[d] = float(np.percentile(arr, q_lo))
            pred_hi[d] = float(np.percentile(arr, q_hi))

    for a in ANCHORS:
        pred_lo[a] = pred_hi[a] = float(tgt_anchors[a])

    meta = {
        "interval_kind": INTERVAL_KIND,
        "interval_alpha": float(alpha),
        "n_pushforward_draws": float(n_draws),
        "span_post_mean_D_lo": float(mu_post[0]),
        "span_post_mean_D_hi": float(mu_post[1]),
        "span_post_var_D_lo": float(cov_post[0, 0]),
        "span_post_var_D_hi": float(cov_post[1, 1]),
        "span_obs_D_lo": float(y_obs[0]),
        "span_obs_D_hi": float(y_obs[1]),
        "meas_ratio": float(model.meas_ratio),
        "cov_scale": float(model.cov_scale),
    }
    return pred_lo, pred_hi, meta


def calibrate_log_span_model(
    df: pd.DataFrame,
    *,
    model: LogSpanModel | None = None,
    alpha: float = PRED_INTERVAL_ALPHA,
    n_draws: int = 300,
    seed: int = RNG_SEED_DEFAULT,
) -> dict[str, Any]:
    """
    LOO diagnostics for the hierarchical span model.

    (i) prior-predictive coverage of (D_lo, D_hi);
    (ii) mf pushforward coverage given pp and prior-predictive D_lo draws.
    """
    complete = df.dropna(subset=[f"tgt_{a}" for a in ANCHORS]).copy()
    if len(complete) < MIN_MODEL_N + 1:
        return {"n": 0, "error": "too few rows"}

    if model is None:
        model = fit_log_span_model(complete, calibrate=True, alpha=alpha)
    if model is None:
        return {"n": 0, "error": "span model fit failed"}

    prior_cov = _loo_prior_predictive_coverage(complete, model, alpha=alpha)

    cover_mf = n = 0
    widths_mf = []
    rng = np.random.default_rng(seed)
    q_lo, q_hi = alpha / 2.0, 1.0 - alpha / 2.0

    for idx, r in complete.iterrows():
        m_loo = fit_log_span_model(
            complete,
            exclude_idx=[idx],
            meas_ratio=model.meas_ratio,
            cov_scale=model.cov_scale,
            calibrate=False,
        )
        if m_loo is None:
            continue
        note = str(r.get("note_target") or r.get("note") or "")
        mu = m_loo.mu_for(octave_from_note(note))
        pred_sd = float(np.sqrt((m_loo.cov + m_loo.meas_cov)[0, 0]))
        draws = rng.normal(mu[0], pred_sd, size=n_draws)
        mf_s = np.exp(float(np.log(r["tgt_pp"])) + draws)
        mf_a, mf_b = np.quantile(mf_s, [q_lo, q_hi])
        truth = float(r["tgt_mf"])
        cover_mf += int(mf_a <= truth <= mf_b)
        widths_mf.append(float((mf_b - mf_a) / truth) if truth else np.nan)
        n += 1

    return {
        "n": n,
        "alpha": alpha,
        "nominal_coverage": 1.0 - alpha,
        "coverage_D_lo": prior_cov.get("coverage_D_lo", float("nan")),
        "coverage_D_hi": prior_cov.get("coverage_D_hi", float("nan")),
        "coverage_mf_pushforward": cover_mf / n if n else float("nan"),
        "median_rel_width_mf": float(np.nanmedian(widths_mf)) if widths_mf else float("nan"),
        "meas_ratio": float(model.meas_ratio),
        "cov_scale": float(model.cov_scale),
        "interval_kind": INTERVAL_KIND,
        "note": (
            "LOO prior-predictive coverage under N(μ_r, Σ_within·cov_scale + Σ_meas); "
            "hyperparameters calibrated to nominal coverage; ladder intervals use conjugate posterior pushforward."
        ),
    }


def _norm_ppf(p: float) -> float:
    """Approximate inverse CDF of standard normal (Acklam) — avoids scipy.stats dependency quirks."""
    # Beasley-Springer-Moro / Acklam approximation
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464858e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    p = float(p)
    if p <= 0.0 or p >= 1.0:
        return float(np.copysign(np.inf, p - 0.5))
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = np.sqrt(-2.0 * np.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = np.sqrt(-2.0 * np.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )


def _safe_float(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v) or v <= 0:
        return None
    return v


def _parse_number(token: str) -> float | None:
    t = str(token).strip().replace("\u00a0", "")
    if not t:
        return None
    if "," in t and "." not in t:
        t = t.replace(",", ".")
    else:
        t = t.replace(",", "")
    return _safe_float(t)


def _split_paste_line(line: str) -> list[str]:
    line = line.strip()
    if not line:
        return []
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if ";" in line:
        return [c.strip() for c in line.split(";")]
    if "," in line:
        parts = [c.strip() for c in line.split(",")]
        if len(parts) >= 4:
            return parts
    return line.split()


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", "_", str(h).strip().lower())


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _stable_note_seed_offset(note: str, *, modulus: int = 10007) -> int:
    """Process-stable offset so interval draws do not depend on PYTHONHASHSEED.

    ``hash(note)`` is salted per interpreter unless PYTHONHASHSEED is fixed,
    which made ``pred_lo`` / ``pred_hi`` non-reproducible across runs.
    """
    digest = hashlib.sha256(str(note).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % int(modulus)


def is_dynamics_panel_filename(path: str | Path) -> bool:
    """
    True for dynamics-panel workbooks (e.g. Violino_dinámicas.xlsx).

    Important: do NOT match bare substring 'din' — it false-positives on
    'ordinario' (Zenodo Arco_ordinario collections).
    """
    name = Path(path).name.lower()
    if "zenodo" in name:
        return False
    # Normalize accented a for matching
    name_ascii = (
        name.replace("á", "a")
        .replace("à", "a")
        .replace("ã", "a")
        .replace("ä", "a")
    )
    return any(
        key in name_ascii
        for key in ("dinamicas", "dinamica", "dynamics", "dynamic_panel", "violino_din")
    )


def discover_panel_xlsx(root: str | Path | None = None) -> Path | None:
    """Pick the best dynamics panel .xlsx under root (never Zenodo)."""
    root = Path(root or r"C:\Users\lmr20\Desktop\Violino - extrapol")
    xlsx = sorted(root.glob("*.xlsx"))
    preferred = [p for p in xlsx if is_dynamics_panel_filename(p)]
    if preferred:
        # Prefer names containing both violin/violino and dinam*
        scored = []
        for p in preferred:
            n = p.name.lower().replace("á", "a")
            score = 0
            if "violino" in n or "violin" in n:
                score += 2
            if "dinam" in n or "dynamic" in n:
                score += 2
            scored.append((score, p))
        scored.sort(key=lambda t: (-t[0], t[1].name.lower()))
        return scored[0][1]
    fallback = [p for p in xlsx if "zenodo" not in p.name.lower()]
    return fallback[0] if fallback else None


def validate_panel_df(df: pd.DataFrame) -> list[str]:
    """Return hard errors (empty list if OK)."""
    errs: list[str] = []
    need = {"note", "tgt_pp", "tgt_mf", "tgt_ff"}
    missing = need - set(df.columns)
    if missing:
        if df.empty and not list(df.columns):
            errs.append(
                "no readable note/pp/mf/ff rows — wrong workbook? "
                "Use Violino_dinámicas.xlsx (IOWA+ORCHIDEA panel), not Zenodo collections."
            )
        else:
            errs.append(f"missing columns: {sorted(missing)}")
        return errs
    if df.empty:
        errs.append("panel has 0 rows")
    notes = df["note"].astype(str).str.strip()
    if notes.duplicated().any():
        dups = sorted(notes[notes.duplicated()].unique().tolist())
        errs.append(f"duplicate note labels: {dups[:12]}")
    for a in ANCHORS:
        col = df[f"tgt_{a}"]
        bad = col.notna() & ((col <= 0) | ~np.isfinite(col.astype(float)))
        if bad.any():
            errs.append(f"non-positive/non-finite tgt_{a}: n={int(bad.sum())}")
    return errs


def parse_paste_panel(text: str) -> pd.DataFrame:
    """Parse pasted note + pp/mf/ff table (header optional)."""
    lines = [ln for ln in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        raise ValueError("Paste is empty — provide note, pp, mf, ff per line.")

    rows_raw = [r for r in (_split_paste_line(ln) for ln in lines) if r]
    header_map = {"note": 0, "pp": 1, "mf": 2, "ff": 3}
    start = 0
    head = [_norm_header(h) for h in rows_raw[0]]
    mapped = [HEADER_ALIASES.get(h, h) for h in head]
    if "note" in mapped and "pp" in mapped and "mf" in mapped and "ff" in mapped:
        header_map = {k: mapped.index(k) for k in ("note", "pp", "mf", "ff")}
        start = 1
    elif len(rows_raw[0]) >= 4 and _parse_number(rows_raw[0][1]) is None:
        start = 1

    records: list[dict] = []
    for r in rows_raw[start:]:
        if len(r) < 4:
            raise ValueError(f"Need 4 columns (note pp mf ff), got {len(r)}: {r}")
        note = str(r[header_map["note"]]).strip()
        if not note:
            continue
        raw_vals = [r[header_map[k]] for k in ("pp", "mf", "ff")]
        pp, mf, ff = (_parse_number(x) for x in raw_vals)
        if any(v is None for v in (pp, mf, ff)):
            raise ValueError(
                f"Invalid/non-positive anchors for note {note!r}: pp={raw_vals[0]!r}, "
                f"mf={raw_vals[1]!r}, ff={raw_vals[2]!r}"
            )
        records.append(
            {
                "note": note,
                "note_target": note,
                "tgt_pp": pp,
                "tgt_mf": mf,
                "tgt_ff": ff,
                "complete_anchors": True,
            }
        )
    if not records:
        raise ValueError("No data rows after parsing paste.")
    df = pd.DataFrame(records)
    errs = validate_panel_df(df)
    # duplicates are warnings for paste merges; keep as ValueError only for empty/cols
    hard = [e for e in errs if not e.startswith("duplicate")]
    if hard:
        raise ValueError("; ".join(hard))
    return df


@dataclass
class ColumnMap:
    """0-based column indices for panel load (optional explicit GUI mapping)."""

    note: int
    pp: int
    mf: int
    ff: int
    note_target: int | None = None
    data_start_row: int = 0  # 0-based first data row
    sheet: str | int | None = 0

    def as_header_map(self) -> dict[str, int]:
        out = {"note": int(self.note), "pp": int(self.pp), "mf": int(self.mf), "ff": int(self.ff)}
        if self.note_target is not None:
            out["note_target"] = int(self.note_target)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "note": self.note,
            "pp": self.pp,
            "mf": self.mf,
            "ff": self.ff,
            "note_target": self.note_target,
            "data_start_row": self.data_start_row,
            "sheet": self.sheet,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ColumnMap":
        return cls(
            note=int(d["note"]),
            pp=int(d["pp"]),
            mf=int(d["mf"]),
            ff=int(d["ff"]),
            note_target=None if d.get("note_target") is None else int(d["note_target"]),
            data_start_row=int(d.get("data_start_row", 0)),
            sheet=d.get("sheet", 0),
        )


def excel_col_letter(index: int) -> str:
    """0-based index → Excel column letters (0→A)."""
    n = int(index) + 1
    letters = []
    while n:
        n, r = divmod(n - 1, 26)
        letters.append(chr(65 + r))
    return "".join(reversed(letters))


def _read_workbook_rows(
    path: Path, sheet: str | int | None = 0
) -> tuple[list[str], list[tuple[Any, ...]], str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = load_workbook(path, data_only=True, read_only=True)
        names = list(wb.sheetnames)
        if sheet is None or sheet == 0:
            sheet_name = names[0]
        elif isinstance(sheet, int):
            sheet_name = names[sheet]
        else:
            sheet_name = str(sheet)
        ws = wb[sheet_name]
        all_rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    return names, all_rows, sheet_name


def inspect_workbook(
    path: str | Path,
    *,
    sheet: str | int | None = 0,
    preview_rows: int = 10,
) -> dict[str, Any]:
    """
    Inspect an Excel workbook for the column-mapping UI.

    Returns sheet names, column labels, preview rows, and a suggested ColumnMap.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Panel file not found: {path}")
    names, all_rows, sheet_name = _read_workbook_rows(path, sheet=sheet)
    ncol = max((len(r) for r in all_rows), default=0)

    # Prefer a detected header row among the first 15
    suggested: ColumnMap | None = None
    header_row_idx: int | None = None
    header_cells: list[Any] = [None] * ncol
    for i, row in enumerate(all_rows[:15]):
        hm = _detect_header_map(row)
        if hm is not None and "note" in hm:
            header_row_idx = i
            header_cells = list(row) + [None] * max(0, ncol - len(row))
            suggested = ColumnMap(
                note=hm["note"],
                pp=hm["pp"],
                mf=hm["mf"],
                ff=hm["ff"],
                note_target=hm.get("note_target"),
                data_start_row=i + 1,
                sheet=sheet_name,
            )
            break

    if suggested is None:
        # Legacy Violino_dinámicas defaults (IOWA block)
        suggested = ColumnMap(
            note=NOTE_COL,
            pp=TARGET_COLS["pp"],
            mf=TARGET_COLS["mf"],
            ff=TARGET_COLS["ff"],
            note_target=TARGET_NOTE_COL,
            data_start_row=2,
            sheet=sheet_name,
        )
        if all_rows:
            header_cells = list(all_rows[min(1, len(all_rows) - 1)]) + [None] * max(
                0, ncol - len(all_rows[min(1, len(all_rows) - 1)])
            )

    col_labels = []
    for j in range(ncol):
        hdr = header_cells[j] if j < len(header_cells) else None
        label = f"{excel_col_letter(j)}"
        if hdr is not None and str(hdr).strip():
            label = f"{excel_col_letter(j)} — {hdr}"
        col_labels.append(label)

    preview = []
    for i, row in enumerate(all_rows[:preview_rows]):
        preview.append(
            {
                "row": i + 1,
                "values": [row[j] if j < len(row) else None for j in range(min(ncol, 16))],
            }
        )

    return {
        "path": str(path),
        "sheets": names,
        "sheet": sheet_name,
        "n_cols": ncol,
        "n_rows": len(all_rows),
        "column_labels": col_labels,
        "header_row_index": header_row_idx,
        "suggested": suggested,
        "preview": preview,
    }


def _rows_from_column_map(
    all_rows: list[tuple[Any, ...]], column_map: ColumnMap
) -> list[dict]:
    hm = column_map.as_header_map()
    need = max(hm.values())
    rows: list[dict] = []
    for row in all_rows[int(column_map.data_start_row) :]:
        if not row or len(row) <= need:
            continue
        note_a = row[hm["note"]]
        if note_a is None or (isinstance(note_a, str) and not note_a.strip()):
            continue
        if isinstance(note_a, (int, float)) and not isinstance(note_a, bool):
            continue
        if not isinstance(note_a, str):
            note_a = str(note_a)
        if "note_target" in hm and len(row) > hm["note_target"]:
            note_t = row[hm["note_target"]]
        else:
            note_t = note_a
        rec: dict = {
            "note": str(note_a).strip(),
            "note_target": str(note_t).strip() if note_t else str(note_a).strip(),
        }
        ok = True
        for d in ANCHORS:
            v = _safe_float(row[hm[d]] if len(row) > hm[d] else None)
            rec[f"tgt_{d}"] = v
            if v is None:
                ok = False
        rec["complete_anchors"] = ok
        if any(rec[f"tgt_{d}"] is not None for d in ANCHORS):
            rows.append(rec)
    return rows


def _detect_header_map(row: Sequence[Any]) -> dict[str, int] | None:
    """
    Map note/pp/mf/ff columns.

    Prefer *exact* headers pp/mf/ff (IOWA+ORCHIDEA block) over aliases such as
    pianissimo/fortissimo, which may appear earlier in Philharmonia columns.
    """
    norms = [_norm_header(c) if c is not None else "" for c in row]

    def _find_exact(name: str) -> int | None:
        hits = [i for i, h in enumerate(norms) if h == name]
        return hits[-1] if hits else None  # rightmost if duplicated

    def _find_aliased(name: str) -> int | None:
        hits = []
        for i, h in enumerate(norms):
            if HEADER_ALIASES.get(h, h) == name and h != name:
                hits.append(i)
        return hits[-1] if hits else None

    pp = _find_exact("pp")
    mf = _find_exact("mf")
    ff = _find_exact("ff")
    if pp is None:
        pp = _find_aliased("pp")
    if mf is None:
        mf = _find_aliased("mf")
    if ff is None:
        ff = _find_aliased("ff")
    if pp is None or mf is None or ff is None:
        return None

    out = {"pp": pp, "mf": mf, "ff": ff}

    # note labels: prefer 'note'/'notas'; target pitch near the IOWA block if present
    for cand in ("note", "notas", "notes", "nota", "pitch"):
        j = _find_exact(cand)
        if j is None:
            j = _find_aliased("note") if cand == "note" else None
        if j is not None:
            out["note"] = j
            break
    for cand in ("note_target", "target_note", "iowa_note"):
        j = _find_exact(cand)
        if j is not None:
            out["note_target"] = j
            break
    # Heuristic: string column immediately left of the pp column (IOWA note labels)
    if "note_target" not in out and pp > 0:
        left = norms[pp - 1] if pp - 1 < len(norms) else ""
        if left in ("", "note_target", "target_note") or left.startswith("note"):
            out["note_target"] = pp - 1
        else:
            # blank header above IOWA notes is common — still use that column if row-2 looks like pitches
            out["note_target"] = pp - 1

    if "note" not in out:
        out["note"] = out.get("note_target", 0)
    return out


def load_panel_xlsx(
    path: str | Path,
    sheet: str | int | None = 0,
    *,
    column_map: ColumnMap | dict[str, Any] | None = None,
) -> pd.DataFrame:
    """
    Load IOWA+ORCHIDEA anchors.

    Prefer an explicit ``column_map`` (from the GUI column picker). Otherwise
    auto-detect headers; fall back to the legacy Violino_dinámicas layout.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Panel file not found: {path}")

    cmap: ColumnMap | None
    if column_map is None:
        cmap = None
    elif isinstance(column_map, ColumnMap):
        cmap = column_map
    else:
        cmap = ColumnMap.from_dict(column_map)

    # Zenodo is OK only when the user explicitly mapped columns
    if "zenodo" in path.name.lower() and cmap is None:
        raise ValueError(
            f"{path.name} looks like a Zenodo technique collection, not the dynamics panel. "
            "Open Map columns… and choose note/pp/mf/ff, or use Violino_dinámicas.xlsx."
        )

    sheet_key: str | int | None = cmap.sheet if cmap is not None else sheet
    _, all_rows, _ = _read_workbook_rows(path, sheet=sheet_key)

    if cmap is not None:
        rows = _rows_from_column_map(all_rows, cmap)
    else:
        header_map: dict[str, int] | None = None
        data_start = 0
        for i, row in enumerate(all_rows[:15]):
            if not row:
                continue
            hm = _detect_header_map(row)
            if hm is not None and "note" in hm:
                header_map = hm
                data_start = i + 1
                break
        if header_map is not None:
            rows = _rows_from_column_map(
                all_rows,
                ColumnMap(
                    note=header_map["note"],
                    pp=header_map["pp"],
                    mf=header_map["mf"],
                    ff=header_map["ff"],
                    note_target=header_map.get("note_target"),
                    data_start_row=data_start,
                    sheet=sheet_key,
                ),
            )
        else:
            # Legacy Violino_dinámicas layout (skip title rows)
            legacy = ColumnMap(
                note=NOTE_COL,
                pp=TARGET_COLS["pp"],
                mf=TARGET_COLS["mf"],
                ff=TARGET_COLS["ff"],
                note_target=TARGET_NOTE_COL,
                data_start_row=2,
                sheet=sheet_key,
            )
            rows = _rows_from_column_map(all_rows, legacy)

    df = pd.DataFrame(rows)
    # Attach upstream fill-panel provenance when the workbook carries it
    df = _attach_panel_provenance(df, path, sheet_key)
    errs = validate_panel_df(df)
    hard = [e for e in errs if not e.startswith("duplicate")]
    if hard:
        raise ValueError(f"Invalid panel {path.name}: " + "; ".join(hard))
    return df


def _attach_panel_provenance(
    df: pd.DataFrame,
    path: Path,
    sheet: str | int | None,
) -> pd.DataFrame:
    """Merge prov_pp/mf/ff and edge_filled_anchor from a headered Panel sheet if present."""
    if df.empty:
        return df
    try:
        raw = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
    except Exception:
        return df
    if raw is None or raw.empty:
        return df
    cols_l = {str(c).strip().lower(): c for c in raw.columns}
    note_col = None
    for cand in ("note", "note_target", "notas", "notes"):
        if cand in cols_l:
            note_col = cols_l[cand]
            break
    if note_col is None:
        return df
    want = ["prov_pp", "prov_mf", "prov_ff", "edge_filled_anchor", "estimator", "edge_k", "edge_taper_r", "cap_engaged"]
    present = {w: cols_l[w] for w in want if w in cols_l}
    # also accept Edge_K casing
    if "edge_k" not in present:
        for k, c in cols_l.items():
            if k.replace(" ", "_") == "edge_k":
                present["edge_k"] = c
    if not present:
        return df
    meta = raw[[note_col] + list(present.values())].copy()
    meta = meta.rename(columns={note_col: "note", **{v: k for k, v in present.items()}})
    meta["note"] = meta["note"].astype(str).str.strip()
    # Map edge_k → keep as edge_K for audit passthrough if needed
    out = df.copy()
    out["note_key"] = out["note"].astype(str).str.strip()
    meta = meta.rename(columns={"note": "note_key"})
    out = out.merge(meta, on="note_key", how="left")
    out = out.drop(columns=["note_key"])
    return out


def target_distance_geometry(tgt_log: dict[str, float]) -> dict[str, float]:
    d_lo = float(tgt_log["mf"] - tgt_log["pp"])
    d_hi = float(tgt_log["ff"] - tgt_log["mf"])
    return {
        "D_lo_log": d_lo,
        "D_hi_log": d_hi,
        "D_total_log": d_lo + d_hi,
        "R_mf_pp": float(np.exp(d_lo)),
        "R_ff_mf": float(np.exp(d_hi)),
        "R_ff_pp": float(np.exp(d_lo + d_hi)),
        "share_lo": float(d_lo / (d_lo + d_hi)) if abs(d_lo + d_hi) > 1e-12 else 0.5,
    }


def corpus_target_distance_summary(df: pd.DataFrame, exclude_idx: Iterable[Any] | None = None) -> dict[str, float]:
    exclude = set(exclude_idx or [])
    d_lo, d_hi, r_mf_pp, r_ff_mf, shares = [], [], [], [], []
    for idx, r in df.iterrows():
        if idx in exclude:
            continue
        if any(pd.isna(r.get(f"tgt_{a}")) for a in ANCHORS):
            continue
        logs = {a: float(np.log(r[f"tgt_{a}"])) for a in ANCHORS}
        g = target_distance_geometry(logs)
        d_lo.append(g["D_lo_log"])
        d_hi.append(g["D_hi_log"])
        r_mf_pp.append(g["R_mf_pp"])
        r_ff_mf.append(g["R_ff_mf"])
        shares.append(g["share_lo"])
    if not d_lo:
        return {}
    return {
        "corpus_median_D_lo_log": float(np.median(d_lo)),
        "corpus_median_D_hi_log": float(np.median(d_hi)),
        "corpus_median_R_mf_pp": float(np.median(r_mf_pp)),
        "corpus_median_R_ff_mf": float(np.median(r_ff_mf)),
        "corpus_median_share_lo": float(np.median(shares)),
        "corpus_n": float(len(d_lo)),
    }


def _pooled_steps(
    d_lo: float,
    d_hi: float,
    corpus_geom: dict[str, float] | None,
) -> tuple[float, float, float]:
    """Return (step_lo, step_hi, pool_weight) with shrink-to-corpus for tiny spans."""
    step_lo = d_lo / float(N_STEPS_LO)
    step_hi = d_hi / float(N_STEPS_HI)
    w = 0.0
    if corpus_geom and corpus_geom.get("corpus_n", 0) >= 3:
        c_lo = float(corpus_geom["corpus_median_D_lo_log"]) / float(N_STEPS_LO)
        c_hi = float(corpus_geom["corpus_median_D_hi_log"]) / float(N_STEPS_HI)
        # stronger pooling when local |D| is tiny relative to corpus
        scale = max(abs(d_lo) + abs(d_hi), POOL_EPS)
        c_scale = max(abs(c_lo) * N_STEPS_LO + abs(c_hi) * N_STEPS_HI, POOL_EPS)
        w = float(np.clip(0.35 * (c_scale / (scale + c_scale)), 0.0, 0.85))
        step_lo = (1.0 - w) * step_lo + w * c_lo
        step_hi = (1.0 - w) * step_hi + w * c_hi
    return step_lo, step_hi, w


def tapered_outer_cum_offset(step: float, k: int, r: float = OUTER_TAPER_R) -> float:
    """
    Cumulative log-offset for the k-th outer level (k=1 → ppp/fff, k=2 → pppp/ffff).

    Successive outer steps are step·r^(i−1); cumulative sum_{i=1..k} step·r^(i−1).
    When r == 1.0 exactly, returns step*k (v1.4 bit-exact path).
    """
    if k <= 0:
        return 0.0
    if r == 1.0:
        return float(step) * float(k)
    # Geometric sum; r≠1
    return float(step) * (1.0 - float(r) ** k) / (1.0 - float(r))


def place_equal_log_ladder(
    tgt_log: dict[str, float],
    *,
    corpus_geom: dict[str, float] | None = None,
    outer_taper_r: float = OUTER_TAPER_R,
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """
    Place all 10 dynamics in log space from IOWA+ORCHIDEA segment lengths.

    Outer levels use tapered steps step·r^(k−1) (default r=OUTER_TAPER_R).
    Pass outer_taper_r=1.0 for v1.4 bit-exact equal outer steps.
    """
    warns = ["mode=iowa_orchidea_equal_log"]
    geom = target_distance_geometry(tgt_log)
    d_lo, d_hi = geom["D_lo_log"], geom["D_hi_log"]
    audit = {**geom}
    if corpus_geom:
        audit.update({k: float(v) for k, v in corpus_geom.items() if isinstance(v, (int, float))})

    out = {"pp": tgt_log["pp"], "mf": tgt_log["mf"], "ff": tgt_log["ff"]}
    for d, frac in UNIFORM_FRAC_LO.items():
        out[d] = tgt_log["pp"] + frac * d_lo
        audit[f"frac_{d}"] = float(frac)
    for d, frac in UNIFORM_FRAC_HI.items():
        out[d] = tgt_log["mf"] + frac * d_hi
        audit[f"frac_{d}"] = float(frac)

    step_lo, step_hi, pool_w = _pooled_steps(d_lo, d_hi, corpus_geom)
    audit["step_lo_log"] = float(step_lo)
    audit["step_hi_log"] = float(step_hi)
    audit["outer_pool_weight"] = float(pool_w)
    audit["outer_taper_r"] = float(outer_taper_r)
    # Soft side: subtract cumulative tapered offsets; loud side: add
    if outer_taper_r == 1.0:
        # v1.4 bit-exact branch (identical expressions)
        out["ppp"] = tgt_log["pp"] - step_lo
        out["pppp"] = tgt_log["pp"] - 2.0 * step_lo
        out["fff"] = tgt_log["ff"] + step_hi
        out["ffff"] = tgt_log["ff"] + 2.0 * step_hi
        warns.append("outer_levels=segment_step_with_optional_corpus_pool")
    else:
        off_lo_1 = tapered_outer_cum_offset(step_lo, 1, outer_taper_r)
        off_lo_2 = tapered_outer_cum_offset(step_lo, 2, outer_taper_r)
        off_hi_1 = tapered_outer_cum_offset(step_hi, 1, outer_taper_r)
        off_hi_2 = tapered_outer_cum_offset(step_hi, 2, outer_taper_r)
        out["ppp"] = tgt_log["pp"] - off_lo_1
        out["pppp"] = tgt_log["pp"] - off_lo_2
        out["fff"] = tgt_log["ff"] + off_hi_1
        out["ffff"] = tgt_log["ff"] + off_hi_2
        warns.append(
            f"outer_levels=segment_step_taper_r={outer_taper_r:.2f}_with_optional_corpus_pool"
        )
    audit["outer_off_lo_k1"] = float(tgt_log["pp"] - out["ppp"])
    audit["outer_off_lo_k2"] = float(tgt_log["pp"] - out["pppp"])
    audit["outer_off_hi_k1"] = float(out["fff"] - tgt_log["ff"])
    audit["outer_off_hi_k2"] = float(out["ffff"] - tgt_log["ff"])
    return out, audit, warns


def _brent_root(f, a: float, b: float, *, tol: float = 1e-14, maxiter: int = 200) -> float | None:
    """Brent–Dekker root on [a,b] if f(a)*f(b)<=0; else None."""
    fa, fb = f(a), f(b)
    if not (np.isfinite(fa) and np.isfinite(fb)):
        return None
    if fa == 0.0:
        return float(a)
    if fb == 0.0:
        return float(b)
    if fa * fb > 0:
        return None
    # scipy-free Brent (Numerical Recipes style, compact)
    x0, x1, x2 = a, b, a
    f0, f1, f2 = fa, fb, fa
    d = e = b - a
    for _ in range(maxiter):
        if f1 * f2 > 0:
            x2, f2 = x0, f0
            e = d = x1 - x0
        if abs(f2) < abs(f1):
            x0, x1, x2 = x1, x2, x1
            f0, f1, f2 = f1, f2, f1
        tol1 = 2.0 * tol * abs(x1) + 0.5 * tol
        xm = 0.5 * (x2 - x1)
        if abs(xm) <= tol1 or f1 == 0.0:
            return float(x1)
        if abs(e) >= tol1 and abs(f0) > abs(f1):
            s = f1 / f0
            if x0 == x2:
                p = 2.0 * xm * s
                q = 1.0 - s
            else:
                q = f0 / f2
                r = f1 / f2
                p = s * (2.0 * xm * q * (q - r) - (x1 - x0) * (r - 1.0))
                q = (q - 1.0) * (r - 1.0) * (s - 1.0)
            if p > 0:
                q = -q
            p = abs(p)
            min1 = 3.0 * xm * q - abs(tol1 * q)
            min2 = abs(e * q)
            if 2.0 * p < min(min1, min2):
                e = d
                d = p / q
            else:
                d = xm
                e = d
        else:
            d = xm
            e = d
        x0, f0 = x1, f1
        x1 = x1 + d if abs(d) > tol1 else x1 + (tol1 if xm > 0 else -tol1)
        f1 = f(x1)
    return float(x1)


TANH_ANCHOR_VERIFY_TOL = 1e-6  # log-space; re-pin absorbs only float dust
# Near-flat log-span threshold (internal_default): one-sided |span| below this
# makes the saturating curvature ratio ill-posed.
TANH_NEAR_FLAT_TOL = 1e-12
TANH_NA_REASON_NONMONO = "non-monotonic anchors"
TANH_NA_REASON_FLAT = "near-flat segment (|span| < tol): saturating fit ill-posed"


def _seg_sign(x: float) -> int:
    if x > 0.0:
        return 1
    if x < 0.0:
        return -1
    return 0


def classify_anchor_log_spans(
    y_pp: float,
    y_mf: float,
    y_ff: float,
    *,
    tol: float = TANH_NEAR_FLAT_TOL,
) -> str:
    """
    Shared span classification (log space) for quality flags and tanh guard.

    Returns:
      ``nonmono`` — strict opposite signs, both |span| ≥ tol
      ``flat_one_sided`` — exactly one |span| < tol (ill-posed for saturating fit)
      ``ok`` — same-sign spans, or both near-flat (constant ladder)
    """
    d_lo, d_hi = float(y_mf) - float(y_pp), float(y_ff) - float(y_mf)
    lo_flat = abs(d_lo) < tol
    hi_flat = abs(d_hi) < tol
    if lo_flat ^ hi_flat:
        return "flat_one_sided"
    if (not lo_flat) and (not hi_flat) and _seg_sign(d_lo) != _seg_sign(d_hi):
        return "nonmono"
    return "ok"


def _tanh_na_result(
    y_pp: float,
    y_mf: float,
    y_ff: float,
    reason: str,
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Anchors kept; all non-anchor levels NaN (inapplicable saturating track)."""
    out = {d: float("nan") for d in DYN_ORDER}
    out["pp"], out["mf"], out["ff"] = float(y_pp), float(y_mf), float(y_ff)
    is_nonmono = reason == TANH_NA_REASON_NONMONO
    is_flat = reason == TANH_NA_REASON_FLAT
    fit = {
        "tanh_a": float("nan"),
        "tanh_b": float("nan"),
        "tanh_c": float("nan"),
        "tanh_idx0": float("nan"),
        "tanh_fit_ok": 0.0,
        "tanh_anchor_err": float("nan"),
        "tanh_method_code": -1.0,
        "tanh_na": 1.0,
        "tanh_na_nonmono": 1.0 if is_nonmono else 0.0,
        "tanh_na_flat": 1.0 if is_flat else 0.0,
        "tanh_near_flat_tol": float(TANH_NEAR_FLAT_TOL),
    }
    return out, fit, [f"mode={LADDER_MODE_TANH}", reason]


def place_tanh_saturating_ladder(
    tgt_log: dict[str, float],
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """
    Third-track ladder: log y = a + b·tanh(c·(idx − idx0)) through pp/mf/ff.

    Applicability (v1.5.2.1): before fitting,
      • exactly one of |y_mf−y_pp|, |y_ff−y_mf| < TANH_NEAR_FLAT_TOL
        → N/A, reason ``near-flat segment (|span| < tol): saturating fit ill-posed``;
      • strict opposite nonzero signs → N/A, reason ``non-monotonic anchors``;
      • both |span| < tol → allowed (flat ladder; linear/constant fit).
    No fit / no corrective re-pin on N/A. ``n_tanh_na_nonmono`` tracks the
    strict-opposite case and must match ``n_nonmonotonic_anchors``.

    Construction when applicable:
      1. Primary: idx0 = idx(mf), hence a = log(mf) (tanh(0)=0 → mf exact).
      2. Solve 1-D for c from collinearity of the three anchors in (tanh, log y).
      3. b closed-form; c→0 is the linear-in-index limit.
      4. Evaluate the raw curve; **verify** anchors within 1e-6 in log space
         (failure → same NaN path). Re-pinning may only absorb float dust.

    Fallback when the mf-centered ratio is outside the tanh range: 1-D search over
    idx0 ∈ (idx_pp, idx_ff). ladder_mode name: "tanh_saturating". Never default.
    """
    warns = [f"mode={LADDER_MODE_TANH}"]
    idx_pp, idx_mf, idx_ff = DYN_LEVEL["pp"], DYN_LEVEL["mf"], DYN_LEVEL["ff"]
    y_pp, y_mf, y_ff = float(tgt_log["pp"]), float(tgt_log["mf"]), float(tgt_log["ff"])

    # --- applicability guard (before any fit); shared with quality nonmono flag ---
    span_cls = classify_anchor_log_spans(y_pp, y_mf, y_ff)
    if span_cls == "flat_one_sided":
        return _tanh_na_result(y_pp, y_mf, y_ff, TANH_NA_REASON_FLAT)
    if span_cls == "nonmono":
        return _tanh_na_result(y_pp, y_mf, y_ff, TANH_NA_REASON_NONMONO)

    def _raw_curve(a: float, b: float, c: float, idx0: float) -> dict[str, float]:
        """Evaluate without touching anchors (verification is separate)."""
        if c == 0.0:
            return {d: float(a + b * (DYN_LEVEL[d] - idx0)) for d in DYN_ORDER}
        return {
            d: float(a + b * np.tanh(c * (DYN_LEVEL[d] - idx0))) for d in DYN_ORDER
        }

    def _fit_centered() -> tuple[float, float, float, float] | None:
        idx0 = idx_mf
        a = y_mf
        d_pp, d_ff = idx_pp - idx0, idx_ff - idx0  # -3, +2
        A, B = y_pp - a, y_ff - a
        if abs(A) < 1e-15 and abs(B) < 1e-15:
            return a, 0.0, 0.0, idx0

        def g(c: float) -> float:
            return float(A * np.tanh(c * d_ff) - B * np.tanh(c * d_pp))

        lin_res = A * d_ff - B * d_pp
        if abs(lin_res) < 1e-12:
            beta = B / d_ff if abs(d_ff) > 1e-15 else (A / d_pp if abs(d_pp) > 1e-15 else 0.0)
            return a, beta, 0.0, idx0

        c_sol = None
        for lo, hi in ((1e-9, 2.0), (-2.0, -1e-9), (2.0, 8.0), (-8.0, -2.0)):
            c_sol = _brent_root(g, lo, hi)
            if c_sol is not None:
                break
        if c_sol is None:
            grid = np.concatenate(
                [-np.geomspace(1e-6, 12.0, 40), np.geomspace(1e-6, 12.0, 40)]
            )
            gs = [g(float(c)) for c in grid]
            for i in range(len(grid) - 1):
                if not (np.isfinite(gs[i]) and np.isfinite(gs[i + 1])):
                    continue
                if gs[i] == 0.0:
                    c_sol = float(grid[i])
                    break
                if gs[i] * gs[i + 1] <= 0:
                    c_sol = _brent_root(g, float(grid[i]), float(grid[i + 1]))
                    if c_sol is not None:
                        break
        if c_sol is None:
            return None
        th_ff = np.tanh(c_sol * d_ff)
        th_pp = np.tanh(c_sol * d_pp)
        if abs(th_ff) >= abs(th_pp) and abs(th_ff) > 1e-15:
            b = B / th_ff
        elif abs(th_pp) > 1e-15:
            b = A / th_pp
        else:
            return None
        return a, float(b), float(c_sol), float(idx0)

    def _fit_free_idx0() -> tuple[float, float, float, float] | None:
        best = None
        best_err = np.inf
        for idx0 in np.linspace(idx_pp + 0.05, idx_ff - 0.05, 61):
            d_pp, d_mf, d_ff = idx_pp - idx0, idx_mf - idx0, idx_ff - idx0

            def coll(c: float) -> float:
                t_pp = np.tanh(c * d_pp)
                t_mf = np.tanh(c * d_mf)
                t_ff = np.tanh(c * d_ff)
                return float((y_mf - y_pp) * (t_ff - t_pp) - (y_ff - y_pp) * (t_mf - t_pp))

            c_sol = None
            for lo, hi in ((1e-6, 3.0), (-3.0, -1e-6), (3.0, 10.0), (-10.0, -3.0)):
                c_sol = _brent_root(coll, lo, hi)
                if c_sol is not None:
                    break
            if c_sol is None:
                continue
            t_pp = np.tanh(c_sol * d_pp)
            t_ff = np.tanh(c_sol * d_ff)
            if abs(t_ff - t_pp) < 1e-15:
                continue
            b = (y_ff - y_pp) / (t_ff - t_pp)
            a = y_pp - b * t_pp
            y_mf_hat = a + b * np.tanh(c_sol * d_mf)
            err = abs(y_mf_hat - y_mf)
            if err < best_err:
                best_err = err
                best = (float(a), float(b), float(c_sol), float(idx0))
                if err < 1e-12:
                    break
        return best

    params = _fit_centered()
    method = "idx0_at_mf_1d_c"
    if params is None:
        params = _fit_free_idx0()
        method = "free_idx0_1d_c"
    if params is None:
        return _tanh_na_result(y_pp, y_mf, y_ff, "tanh_na_fit_failed")

    a, b, c, idx0 = params
    raw = _raw_curve(a, b, c, idx0)
    if c == 0.0:
        warns.append("tanh_degenerate_to_linear_in_index")

    # Verification (not corrective re-pin): raw curve must hit anchors in log space
    max_anchor_err = max(
        abs(raw["pp"] - y_pp), abs(raw["mf"] - y_mf), abs(raw["ff"] - y_ff)
    )
    if max_anchor_err > TANH_ANCHOR_VERIFY_TOL:
        return _tanh_na_result(y_pp, y_mf, y_ff, "tanh_na_anchor_verification_failed")

    # Dust-only re-pin (anchors already within tol)
    out = dict(raw)
    out["pp"], out["mf"], out["ff"] = y_pp, y_mf, y_ff
    fit = {
        "tanh_a": float(a),
        "tanh_b": float(b),
        "tanh_c": float(c),
        "tanh_idx0": float(idx0),
        "tanh_fit_ok": 1.0,
        "tanh_anchor_err": float(max_anchor_err),
        "tanh_method_code": 1.0 if method.startswith("idx0_at_mf") else 2.0,
        "tanh_na": 0.0,
        "tanh_na_nonmono": 0.0,
        "tanh_na_flat": 0.0,
        "tanh_near_flat_tol": float(TANH_NEAR_FLAT_TOL),
    }
    warns.append(f"tanh_fit={method}")
    return out, fit, warns


def pchip_intermediates_from_anchors(tgt_log: dict[str, float]) -> dict[str, float]:
    """PCHIP through the three measured anchors; evaluate at p/mp/f only."""
    xs = np.array([DYN_LEVEL[a] for a in ANCHORS], dtype=float)
    ys = np.array([tgt_log[a] for a in ANCHORS], dtype=float)
    spline = PchipInterpolator(xs, ys, extrapolate=False)
    return {d: float(spline(DYN_LEVEL[d])) for d in INTERPOLATED}


def note_quality_and_flags(
    tgt_anchors: dict[str, float],
    geom: dict[str, float],
    *,
    anchor_provenance: dict[str, str] | None = None,
) -> tuple[float, list[str]]:
    flags: list[str] = []
    pp, mf, ff = tgt_anchors["pp"], tgt_anchors["mf"], tgt_anchors["ff"]
    # Align with tanh guard: strict opposite log-spans (both |span| ≥ TANH_NEAR_FLAT_TOL)
    span_cls = classify_anchor_log_spans(float(np.log(pp)), float(np.log(mf)), float(np.log(ff)))
    if span_cls == "nonmono":
        flags.append("nonmonotonic_anchors")
    if geom["D_lo_log"] * geom["D_hi_log"] < 0:
        flags.append("opposite_segment_signs")
    if abs(geom["D_total_log"]) < 1e-6:
        flags.append("near_zero_total_span")
    if min(pp, mf, ff) / max(pp, mf, ff) > 0.98:
        flags.append("flat_dynamic_span")

    prov = {a: str((anchor_provenance or {}).get(a, "measured")).strip().lower() for a in ANCHORS}
    if any(p == "edge_filled" for p in prov.values()):
        flags.append(EDGE_FILLED_FLAG)
    elif any(p == "interior_fill" for p in prov.values()):
        flags.append(INTERIOR_FILLED_FLAG)

    q = 1.0
    if "nonmonotonic_anchors" in flags:
        q -= 0.35
    if "opposite_segment_signs" in flags:
        q -= 0.15
    if "near_zero_total_span" in flags:
        q -= 0.25
    if "flat_dynamic_span" in flags:
        q -= 0.10
    if EDGE_FILLED_FLAG in flags:
        q -= EDGE_FILLED_QUALITY_MALUS
    elif INTERIOR_FILLED_FLAG in flags:
        q -= INTERIOR_FILLED_QUALITY_MALUS
    # reward clear span
    span = abs(geom["D_total_log"])
    q += float(np.clip(span / 1.0, 0.0, 0.15))
    return float(np.clip(q, 0.05, 1.0)), flags


def _anchor_kinds_from_provenance(anchor_provenance: dict[str, str] | None) -> dict[str, str]:
    """Map upstream panel provenance → value_kind for anchors (SDA / hygiene)."""
    kinds = {}
    prov = anchor_provenance or {}
    for a in ANCHORS:
        p = str(prov.get(a, "measured")).strip().lower()
        if p == "edge_filled":
            kinds[a] = "edge_filled_anchor"
        elif p == "interior_fill":
            kinds[a] = "interior_filled_anchor"
        else:
            kinds[a] = "measured_anchor"
    return kinds


def _rising_teacher_outer_steps(
    df: pd.DataFrame | None,
) -> dict[str, dict[int | None, float]]:
    """
    Register-wise median positive outer steps from notes with pp<=mf<=ff
    (literature-compatible teachers: R1 + R5).
    """
    out: dict[str, dict[int | None, float]] = {"step_lo": {}, "step_hi": {}}
    if df is None or df.empty:
        return out
    by_lo: dict[int | None, list[float]] = {}
    by_hi: dict[int | None, list[float]] = {}
    global_lo, global_hi = [], []
    for _, r in df.iterrows():
        if any(pd.isna(r.get(f"tgt_{a}")) for a in ANCHORS):
            continue
        pp, mf, ff = float(r["tgt_pp"]), float(r["tgt_mf"]), float(r["tgt_ff"])
        if not (pp <= mf <= ff):
            continue
        d_lo = np.log(mf) - np.log(pp)
        d_hi = np.log(ff) - np.log(mf)
        if d_lo <= 0 or d_hi <= 0:
            continue
        step_lo = d_lo / float(N_STEPS_LO)
        step_hi = d_hi / float(N_STEPS_HI)
        note = str(r.get("note_target") or r.get("note") or "")
        octv = octave_from_note(note)
        by_lo.setdefault(octv, []).append(step_lo)
        by_hi.setdefault(octv, []).append(step_hi)
        global_lo.append(step_lo)
        global_hi.append(step_hi)
    if global_lo:
        out["step_lo"][None] = float(np.median(global_lo))
        out["step_hi"][None] = float(np.median(global_hi))
    for octv, vals in by_lo.items():
        if vals:
            out["step_lo"][octv] = float(np.median(vals))
    for octv, vals in by_hi.items():
        if vals:
            out["step_hi"][octv] = float(np.median(vals))
    return out


def acoustics_regularize_ladder(
    tgt_anchors: dict[str, float],
    data_pred: dict[str, float],
    *,
    note_target: str = "",
    teacher_steps: dict[str, dict[int | None, float]] | None = None,
    outer_taper_r: float = OUTER_TAPER_R,
) -> tuple[dict[str, float], list[str], list[str]]:
    """
    Literature-conditioned second track.

    Keeps measured anchors exact. Regularizes imputed/extrapolated cells using
    ACOUSTICS_LITERATURE_RULES (Schoonderwaldt, Schelleng, Meyer, Cremer/Guettler).
    Outer steps use R7 geometric taper step·r^(k−1); R3 soft-cap is the final guard.
    """
    rules: list[str] = ["R6_anchors_inviolable"]
    notes: list[str] = []
    pred = {d: float(data_pred[d]) for d in DYN_ORDER}
    for a in ANCHORS:
        pred[a] = float(tgt_anchors[a])

    pp, mf, ff = float(tgt_anchors["pp"]), float(tgt_anchors["mf"]), float(tgt_anchors["ff"])
    log_pp, log_mf, log_ff = np.log(pp), np.log(mf), np.log(ff)
    d_lo, d_hi = log_mf - log_pp, log_ff - log_mf
    mono_up = pp <= mf <= ff
    mono_down = pp >= mf >= ff

    # --- R1: force→brightness monotone path for imputed interiors (anchors fixed) ---
    rules.append("R1_force_brightness_direction")
    if mono_up or mono_down:
        # Piecewise equal-log already respects segment endpoints; enforce order explicitly
        lo_a, lo_b = (pp, mf) if mono_up else (mf, pp)
        hi_a, hi_b = (mf, ff) if mono_up else (ff, mf)
        # Place p, mp by equal-log (literature-compatible enrichment path along measured span)
        pred["p"] = float(np.exp(log_pp + (1.0 / 3.0) * d_lo))
        pred["mp"] = float(np.exp(log_pp + (2.0 / 3.0) * d_lo))
        pred["f"] = float(np.exp(log_mf + 0.5 * d_hi))
        # clip into segment (numerical safety)
        pred["p"] = float(np.clip(pred["p"], min(lo_a, lo_b), max(lo_a, lo_b)))
        pred["mp"] = float(np.clip(pred["mp"], min(lo_a, lo_b), max(lo_a, lo_b)))
        pred["f"] = float(np.clip(pred["f"], min(hi_a, hi_b), max(hi_a, hi_b)))
        if mono_up:
            pred["p"] = min(pred["p"], pred["mp"])
            pred["mp"] = max(pred["p"], pred["mp"])
            notes.append("R1: monotone-increasing anchors → enrichment path pp→p→mp→mf→f→ff")
        else:
            notes.append("R1: monotone-decreasing anchors → path follows measured descent (rare)")
    else:
        # Non-mono anchors: cannot impose global force law through anchors (R2)
        # Keep segment-wise equal-log interiors (data-faithful within each measured span)
        pred["p"] = float(np.exp(log_pp + (1.0 / 3.0) * d_lo))
        pred["mp"] = float(np.exp(log_pp + (2.0 / 3.0) * d_lo))
        pred["f"] = float(np.exp(log_mf + 0.5 * d_hi))
        notes.append(
            "R1/R2: non-monotonic anchors — interiors stay segment equal-log; "
            "no global rising override of measured pp/mf/ff"
        )

    # --- R2 + R4 + R5 + R7: outer steps with geometric taper ---
    rules.extend(
        [
            "R2_schelleng_control_region",
            "R4_helmholtz_corner_enrichment",
            "R5_register_modulation",
            "R7_outer_taper_compression",
        ]
    )
    octv = octave_from_note(note_target)
    teachers = teacher_steps or {"step_lo": {}, "step_hi": {}}
    teach_lo = teachers.get("step_lo", {}).get(octv)
    if teach_lo is None:
        teach_lo = teachers.get("step_lo", {}).get(None)
    teach_hi = teachers.get("step_hi", {}).get(octv)
    if teach_hi is None:
        teach_hi = teachers.get("step_hi", {}).get(None)

    # Local steps from measured spans
    local_lo = d_lo / float(N_STEPS_LO)
    local_hi = d_hi / float(N_STEPS_HI)

    # Literature-preferred positive enrichment step (R1/R4)
    pos_lo = max(abs(local_lo), ACOUSTICS_MIN_POS_STEP)
    pos_hi = max(abs(local_hi), ACOUSTICS_MIN_POS_STEP)
    if teach_lo is not None:
        pos_lo = max(float(teach_lo), ACOUSTICS_MIN_POS_STEP)
    if teach_hi is not None:
        pos_hi = max(float(teach_hi), ACOUSTICS_MIN_POS_STEP)

    shrink = ACOUSTICS_OUTER_SHRINK_MONO if mono_up else ACOUSTICS_OUTER_SHRINK_NONMONO
    r = float(outer_taper_r)

    def _apply_outers(step_lo_use: float, step_hi_use: float, *, rising: bool, soft: float, loud: float) -> None:
        off_lo_1 = tapered_outer_cum_offset(step_lo_use, 1, r)
        off_lo_2 = tapered_outer_cum_offset(step_lo_use, 2, r)
        off_hi_1 = tapered_outer_cum_offset(step_hi_use, 1, r)
        off_hi_2 = tapered_outer_cum_offset(step_hi_use, 2, r)
        if rising:
            pred["ppp"] = float(np.exp(np.log(soft) - off_lo_1))
            pred["pppp"] = float(np.exp(np.log(soft) - off_lo_2))
            pred["fff"] = float(np.exp(np.log(loud) + off_hi_1))
            pred["ffff"] = float(np.exp(np.log(loud) + off_hi_2))
        else:
            pred["ppp"] = float(np.exp(np.log(soft) + off_lo_1))
            pred["pppp"] = float(np.exp(np.log(soft) + off_lo_2))
            pred["fff"] = float(np.exp(np.log(loud) - off_hi_1))
            pred["ffff"] = float(np.exp(np.log(loud) - off_hi_2))

    if mono_up:
        # Continue enrichment beyond pp/ff (force↑ → brighter)
        step_lo_use = abs((1.0 - shrink) * local_lo + shrink * pos_lo)
        step_hi_use = abs((1.0 - shrink) * local_hi + shrink * pos_hi)
        if r == 1.0:
            pred["ppp"] = float(np.exp(log_pp - step_lo_use))
            pred["pppp"] = float(np.exp(log_pp - 2.0 * step_lo_use))
            pred["fff"] = float(np.exp(log_ff + step_hi_use))
            pred["ffff"] = float(np.exp(log_ff + 2.0 * step_hi_use))
        else:
            _apply_outers(step_lo_use, step_hi_use, rising=True, soft=pp, loud=ff)
        notes.append(
            f"R2/R4/R5/R7: outer enrichment steps shrink={shrink:.2f} taper_r={r:.2f} (rising teachers/register)"
        )
    elif mono_down:
        # Measured descent: continue soft/loud sides consistently but shrink magnitude (Schelleng caution)
        step_lo_use = (1.0 - shrink) * abs(local_lo) + shrink * pos_lo
        step_hi_use = (1.0 - shrink) * abs(local_hi) + shrink * pos_hi
        if r == 1.0:
            pred["ppp"] = float(np.exp(log_pp + step_lo_use))
            pred["pppp"] = float(np.exp(log_pp + 2.0 * step_lo_use))
            pred["fff"] = float(np.exp(log_ff - step_hi_use))
            pred["ffff"] = float(np.exp(log_ff - 2.0 * step_hi_use))
        else:
            _apply_outers(step_lo_use, step_hi_use, rising=False, soft=pp, loud=ff)
        notes.append(f"R2/R7: monotone-down anchors — outer continuation shrunk, taper_r={r:.2f}")
    else:
        # Inconsistent anchors: Schelleng — do not trust local segment for far extrapolation
        rules.append("R2_schelleng_control_region")
        step_lo_use = (1.0 - shrink) * abs(local_lo) + shrink * pos_lo
        step_hi_use = (1.0 - shrink) * abs(local_hi) + shrink * pos_hi
        soft_anchor = min(pp, mf)
        loud_anchor = max(mf, ff)
        if r == 1.0:
            pred["ppp"] = float(soft_anchor * np.exp(-step_lo_use))
            pred["pppp"] = float(soft_anchor * np.exp(-2.0 * step_lo_use))
            pred["fff"] = float(loud_anchor * np.exp(step_hi_use))
            pred["ffff"] = float(loud_anchor * np.exp(2.0 * step_hi_use))
        else:
            _apply_outers(step_lo_use, step_hi_use, rising=True, soft=soft_anchor, loud=loud_anchor)
        notes.append(
            "R2/R4/R7: non-mono anchors — outers from soft/loud extremes with literature positive steps "
            f"(shrink={shrink:.2f}, taper_r={r:.2f}); interiors not forced through a false global rise"
        )

    # --- R3: soft-cap outer growth vs anchors (spectrum ≠ SPL); final guard after taper ---
    rules.append("R3_spectrum_not_spl")
    soft_cap_applied = False
    pre_cap = {d: pred[d] for d in ("pppp", "ppp", "fff", "ffff")}
    for d, center in (("ffff", ff), ("pppp", pp)):
        if center <= 0:
            continue
        ratio = pred[d] / center
        if ratio > ACOUSTICS_RATIO_SOFT_CAP:
            pred[d] = float(center * ACOUSTICS_RATIO_SOFT_CAP)
            soft_cap_applied = True
            notes.append(f"R3: soft-capped {d}/anchor ratio at {ACOUSTICS_RATIO_SOFT_CAP}")
        elif ratio < 1.0 / ACOUSTICS_RATIO_SOFT_CAP:
            pred[d] = float(center / ACOUSTICS_RATIO_SOFT_CAP)
            soft_cap_applied = True
            notes.append(f"R3: soft-capped {d}/anchor inverse ratio at {ACOUSTICS_RATIO_SOFT_CAP}")
    # also cap fff/ppp mildly relative to ff/pp
    if ff > 0 and pred["fff"] / ff > ACOUSTICS_RATIO_SOFT_CAP:
        pred["fff"] = float(ff * np.sqrt(ACOUSTICS_RATIO_SOFT_CAP))
        soft_cap_applied = True
        notes.append(f"R3: soft-capped fff/ff at sqrt({ACOUSTICS_RATIO_SOFT_CAP})")
    if pred["ppp"] > 0 and pp / pred["ppp"] > ACOUSTICS_RATIO_SOFT_CAP:
        pred["ppp"] = float(pp / np.sqrt(ACOUSTICS_RATIO_SOFT_CAP))
        soft_cap_applied = True
        notes.append(f"R3: soft-capped pp/ppp at sqrt({ACOUSTICS_RATIO_SOFT_CAP})")
    if not soft_cap_applied:
        notes.append(
            f"R3: taper alone sufficed (r={r:.2f}; no soft-cap applied; "
            f"pre_cap pppp={pre_cap['pppp']:.6g}, ffff={pre_cap['ffff']:.6g})"
        )

    # Final: anchors exact
    for a in ANCHORS:
        pred[a] = float(tgt_anchors[a])

    # Ensure positivity
    for d in DYN_ORDER:
        if not np.isfinite(pred[d]) or pred[d] <= 0:
            pred[d] = float(data_pred.get(d, tgt_anchors["mf"]))

    return pred, rules, notes


def transfer_one_note(
    tgt_anchors: dict[str, float],
    *,
    use_pchip: bool = False,
    corpus_geom: dict[str, float] | None = None,
    note: str = "",
    note_target: str = "",
    span_model: LogSpanModel | None = None,
    n_pushforward: int = N_PUSHFORWARD_DRAWS,
    interval_alpha: float = PRED_INTERVAL_ALPHA,
    seed: int = RNG_SEED_DEFAULT,
    teacher_steps: dict[str, dict[int | None, float]] | None = None,
    outer_taper_r: float = OUTER_TAPER_R,
    anchor_provenance: dict[str, str] | None = None,
) -> NoteResult:
    warns: list[str] = []
    empty = {d: float("nan") for d in DYN_ORDER}
    if any(tgt_anchors.get(a) is None or tgt_anchors[a] <= 0 for a in ANCHORS):
        return NoteResult(
            note,
            note_target,
            {a: float(tgt_anchors.get(a) or float("nan")) for a in ANCHORS},
            empty,
            empty,
            empty,
            {},
            "skipped_target",
            ["missing anchors"],
            0.0,
            ["incomplete_anchors"],
            interval_kind="",
        )

    tgt_log = {a: float(np.log(tgt_anchors[a])) for a in ANCHORS}
    pred_log, audit, w = place_equal_log_ladder(
        tgt_log, corpus_geom=corpus_geom, outer_taper_r=outer_taper_r
    )
    warns.extend(w)

    if use_pchip:
        inter = pchip_intermediates_from_anchors(tgt_log)
        pred_log.update(inter)
        warns.append("intermediates=PCHIP_from_3_anchors")
    else:
        warns.append("intermediates=equal_log_fractions")

    for a in ANCHORS:
        pred_log[a] = tgt_log[a]

    quality, flags = note_quality_and_flags(
        tgt_anchors, audit, anchor_provenance=anchor_provenance,
    )
    if flags:
        warns.extend(flags)
    if EDGE_FILLED_FLAG in flags:
        warns.append("upstream_panel=edge_filled_anchors")
        audit["edge_filled_anchor"] = 1.0
    else:
        audit["edge_filled_anchor"] = 0.0

    pred = {d: float(np.exp(pred_log[d])) for d in DYN_ORDER}
    for a in ANCHORS:
        pred[a] = float(tgt_anchors[a])

    interval_kind = "none"
    if span_model is not None:
        note_for_oct = note_target or note
        pred_lo, pred_hi, imeta = pushforward_predictive_intervals(
            tgt_anchors,
            span_model,
            octave=octave_from_note(note_for_oct),
            use_pchip=use_pchip,
            corpus_geom=corpus_geom,
            n_draws=n_pushforward,
            alpha=interval_alpha,
            seed=seed + _stable_note_seed_offset(note_for_oct),
            outer_taper_r=outer_taper_r,
        )
        audit.update(imeta)
        interval_kind = INTERVAL_KIND
        warns.append(f"intervals={INTERVAL_KIND}_90pct")
    else:
        # Fallback: degenerate intervals at the point estimate
        pred_lo = dict(pred)
        pred_hi = dict(pred)
        warns.append("intervals=unavailable_no_span_model")

    kinds = {}
    anchor_kinds = _anchor_kinds_from_provenance(anchor_provenance)
    for d in DYN_ORDER:
        if d in ANCHORS:
            kinds[d] = anchor_kinds[d]
        elif d in INTERPOLATED:
            kinds[d] = "interpolated"
        else:
            kinds[d] = "extrapolated_beyond"

    ac_pred, ac_rules, ac_notes = acoustics_regularize_ladder(
        {a: float(tgt_anchors[a]) for a in ANCHORS},
        pred,
        note_target=note_target or note,
        teacher_steps=teacher_steps,
        outer_taper_r=outer_taper_r,
    )
    warns.append("acoustics_prior=literature_regularized_second_track")

    tanh_log, tanh_fit, tanh_warns = place_tanh_saturating_ladder(tgt_log)
    warns.extend(tanh_warns)
    tanh_pred: dict[str, float] = {}
    for d in DYN_ORDER:
        if d in ANCHORS:
            # Anchors always the measured linear values (inapplicable track keeps them)
            tanh_pred[d] = float(tgt_anchors[d])
        elif not np.isfinite(tanh_log.get(d, float("nan"))):
            tanh_pred[d] = float("nan")
        else:
            tanh_pred[d] = float(np.exp(tanh_log[d]))
    warns.append("tanh_saturating=third_track_not_default")

    return NoteResult(
        note=note,
        note_target=note_target,
        target_measured={a: float(tgt_anchors[a]) for a in ANCHORS},
        target_pred=pred,
        pred_lo=pred_lo,
        pred_hi=pred_hi,
        value_kind=kinds,
        status="ok",
        warnings=warns,
        quality=quality,
        flags=flags,
        audit={**audit, **{k: v for k, v in tanh_fit.items() if isinstance(v, (int, float))}},
        interval_kind=interval_kind,
        target_pred_acoustics=ac_pred,
        acoustics_rules_applied=ac_rules,
        acoustics_notes=ac_notes,
        target_pred_tanh=tanh_pred,
        tanh_fit=tanh_fit,
    )


def run_transfer(
    df: pd.DataFrame,
    use_pchip: bool = False,
    *,
    span_model: LogSpanModel | None = None,
    n_pushforward: int = N_PUSHFORWARD_DRAWS,
    interval_alpha: float = PRED_INTERVAL_ALPHA,
    seed: int = RNG_SEED_DEFAULT,
) -> list[NoteResult]:
    corpus_geom = corpus_target_distance_summary(df)
    if span_model is None:
        span_model = fit_log_span_model(df, calibrate=True)
    teacher_steps = _rising_teacher_outer_steps(df)
    results: list[NoteResult] = []
    for i, (_, r) in enumerate(df.iterrows()):
        note = str(r.get("note") or "")
        note_t = str(r.get("note_target") or note)
        tgt = {a: r[f"tgt_{a}"] for a in ANCHORS}
        if any(pd.isna(tgt[a]) for a in ANCHORS):
            results.append(
                NoteResult(
                    note,
                    note_t,
                    {a: float(tgt[a]) if pd.notna(tgt[a]) else float("nan") for a in ANCHORS},
                    {},
                    {},
                    {},
                    {},
                    "skipped_incomplete",
                    ["incomplete IOWA+ORCHIDEA anchors"],
                    0.0,
                    ["incomplete_anchors"],
                )
            )
            continue
        prov = {}
        for a in ANCHORS:
            key = f"prov_{a}"
            if key in r.index and pd.notna(r.get(key)):
                prov[a] = str(r.get(key)).strip().lower()
        if r.get("edge_filled_anchor") in (1, 1.0, True, "1", "true", "True"):
            # Row-level flag from extrapol_data --fill-panel
            for a in ANCHORS:
                prov.setdefault(a, "edge_filled")
        results.append(
            transfer_one_note(
                {a: float(tgt[a]) for a in ANCHORS},
                use_pchip=use_pchip,
                corpus_geom=corpus_geom,
                note=note,
                note_target=note_t,
                span_model=span_model,
                n_pushforward=n_pushforward,
                interval_alpha=interval_alpha,
                seed=seed + i,
                teacher_steps=teacher_steps,
                anchor_provenance=prov or None,
            )
        )
    return results


def predict_held_anchor(
    known_tgt: dict[str, float],
    *,
    hold: str,
    method: str = "production_aligned",
    corpus_geom: dict[str, float] | None = None,
) -> float | None:
    """
    Predict a held-out anchor from the other two.

    production_aligned: uses corpus median share_lo / segment lengths (same geometry
    family as equal-log production when an anchor is missing).
    log_midpoint: geometric mean of the two endpoints (mf) or reflection (pp/ff).
    linear: arithmetic in linear metric space.
    """
    if hold not in ANCHORS:
        raise ValueError(hold)
    method = method.lower()

    if hold == "mf":
        if "pp" not in known_tgt or "ff" not in known_tgt:
            return None
        pp, ff = known_tgt["pp"], known_tgt["ff"]
        if method == "linear":
            return float(0.5 * (pp + ff))
        log_pp, log_ff = np.log(pp), np.log(ff)
        if method == "log_midpoint":
            return float(np.exp(0.5 * (log_pp + log_ff)))
        # production_aligned: place mf by corpus share of total log span
        share = 0.5
        if corpus_geom and "corpus_median_share_lo" in corpus_geom:
            share = float(corpus_geom["corpus_median_share_lo"])
        share = float(np.clip(share, 0.05, 0.95))
        return float(np.exp(log_pp + share * (log_ff - log_pp)))

    if "mf" not in known_tgt:
        return None
    mf = known_tgt["mf"]
    log_mf = np.log(mf)

    if hold == "pp":
        if "ff" not in known_tgt:
            return None
        ff = known_tgt["ff"]
        if method == "linear":
            # mf = pp + share*(ff-pp) → pp = (mf - share*ff)/(1-share)
            share = float((corpus_geom or {}).get("corpus_median_share_lo", 0.5))
            share = float(np.clip(share, 0.05, 0.95))
            return float((mf - share * ff) / (1.0 - share))
        if method == "log_midpoint":
            return float(np.exp(2.0 * log_mf - np.log(ff)))
        # production_aligned: D_hi from mf–ff; D_lo from corpus median ratio of spans
        d_hi = np.log(ff) - log_mf
        if corpus_geom and corpus_geom.get("corpus_median_D_lo_log") is not None:
            d_lo = float(corpus_geom["corpus_median_D_lo_log"])
            # orient sign to match observed hi segment when possible
            if d_hi < 0 and d_lo > 0:
                d_lo = -abs(d_lo)
            elif d_hi > 0 and d_lo < 0:
                d_lo = abs(d_lo)
        else:
            d_lo = d_hi  # fall back to equal segments
        return float(np.exp(log_mf - d_lo))

    if hold == "ff":
        if "pp" not in known_tgt:
            return None
        pp = known_tgt["pp"]
        if method == "linear":
            share = float((corpus_geom or {}).get("corpus_median_share_lo", 0.5))
            share = float(np.clip(share, 0.05, 0.95))
            # mf = pp + share*(ff-pp) → ff = pp + (mf-pp)/share
            return float(pp + (mf - pp) / share)
        if method == "log_midpoint":
            return float(np.exp(2.0 * log_mf - np.log(pp)))
        d_lo = log_mf - np.log(pp)
        if corpus_geom and corpus_geom.get("corpus_median_D_hi_log") is not None:
            d_hi = float(corpus_geom["corpus_median_D_hi_log"])
            if d_lo < 0 and d_hi > 0:
                d_hi = -abs(d_hi)
            elif d_lo > 0 and d_hi < 0:
                d_hi = abs(d_hi)
        else:
            d_hi = d_lo
        return float(np.exp(log_mf + d_hi))
    return None


def _err_summary(arr: Sequence[float]) -> dict[str, float]:
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {}
    return {
        "n": float(a.size),
        "mae": float(np.mean(a)),
        "median_ae": float(np.median(a)),
        "rmse": float(np.sqrt(np.mean(a**2))),
        "p90": float(np.quantile(a, 0.9)),
    }


def holdout_anchor_diagnostics(
    df: pd.DataFrame,
    hold: str = "mf",
    *,
    method: str = "production_aligned",
    leave_one_out_corpus: bool = True,
) -> dict:
    if hold not in ANCHORS:
        raise ValueError(hold)

    abs_err, rel_err, log_err, rows_out = [], [], [], []
    global_geom = corpus_target_distance_summary(df)

    for idx, r in df.iterrows():
        if any(pd.isna(r[f"tgt_{a}"]) for a in ANCHORS):
            continue
        truth = float(r[f"tgt_{hold}"])
        known = {a: float(r[f"tgt_{a}"]) for a in ANCHORS if a != hold}
        geom = (
            corpus_target_distance_summary(df, exclude_idx=[idx])
            if leave_one_out_corpus
            else global_geom
        )
        pred = predict_held_anchor(known, hold=hold, method=method, corpus_geom=geom)
        if pred is None or not np.isfinite(pred) or pred <= 0:
            continue
        ae = abs(pred - truth)
        re = ae / truth if truth else np.nan
        le = abs(np.log(pred) - np.log(truth))
        abs_err.append(ae)
        rel_err.append(re)
        log_err.append(le)
        note = r.get("note_target") if pd.notna(r.get("note_target")) else r.get("note")
        rows_out.append(
            {
                "note": note,
                "hold": hold,
                "method": method,
                "truth": truth,
                "pred": pred,
                "abs_err": ae,
                "rel_err": re,
                "abs_log_err": le,
            }
        )

    return {
        "hold": hold,
        "method": method,
        "abs_err": _err_summary(abs_err),
        "rel_err": _err_summary(rel_err),
        "abs_log_err": _err_summary(log_err),
        "rows": rows_out,
    }


def holdout_all_baselines(df: pd.DataFrame) -> list[dict]:
    methods = ("production_aligned", "log_midpoint", "linear")
    out = []
    for hold in ANCHORS:
        for m in methods:
            out.append(holdout_anchor_diagnostics(df, hold=hold, method=m))
    return out


def leave_one_note_out_register(
    df: pd.DataFrame,
    *,
    use_pchip: bool = False,
) -> dict:
    """
    For each note, rebuild corpus geom without that note and re-impute intermediates.
    Reports stability of p/mp/f (log MAE vs full-corpus imputation).
    """
    full = run_transfer(df, use_pchip=use_pchip)
    full_map = {r.note_target or r.note: r for r in full if r.status == "ok"}
    diffs = {d: [] for d in INTERPOLATED}
    n = 0
    for idx, row in df.iterrows():
        if any(pd.isna(row[f"tgt_{a}"]) for a in ANCHORS):
            continue
        note_t = str(row.get("note_target") or row.get("note") or "")
        geom = corpus_target_distance_summary(df, exclude_idx=[idx])
        anchors = {a: float(row[f"tgt_{a}"]) for a in ANCHORS}
        alt = transfer_one_note(
            anchors, use_pchip=use_pchip, corpus_geom=geom, note=note_t, note_target=note_t
        )
        base = full_map.get(note_t)
        if base is None or alt.status != "ok":
            continue
        n += 1
        for d in INTERPOLATED:
            diffs[d].append(abs(np.log(alt.target_pred[d]) - np.log(base.target_pred[d])))
    return {
        "n": n,
        "log_mae_vs_full_corpus": {d: float(np.mean(v)) if v else float("nan") for d, v in diffs.items()},
        "note": "LOO affects only corpus-pooled outer steps / hold-out geom; equal-log interiors identical without pooling on interiors.",
    }


def bootstrap_diagnostics(
    df: pd.DataFrame,
    *,
    n_boot: int = BOOTSTRAP_DEFAULT,
    seed: int = RNG_SEED_DEFAULT,
    use_pchip: bool = False,
) -> dict:
    """Bootstrap CIs for corpus ratios and production-aligned hold-out MAE."""
    del use_pchip  # hold-out geometry does not use PCHIP on intermediates
    complete = df.dropna(subset=[f"tgt_{a}" for a in ANCHORS]).reset_index(drop=True)
    if len(complete) < 5:
        return {"n_boot": 0, "error": "need >=5 complete rows"}

    rng = np.random.default_rng(seed)
    keys_hold = [(h, m) for h in ANCHORS for m in ("production_aligned",)]
    bags: dict[str, list[float]] = {
        "med_R_ff_pp": [],
        "med_share_lo": [],
        "hold_mf_rel_mae": [],
        "hold_mf_log_mae": [],
        "hold_pp_rel_mae": [],
        "hold_ff_rel_mae": [],
    }

    n = len(complete)
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        sample = complete.iloc[idx].reset_index(drop=True)
        logs = []
        for _, r in sample.iterrows():
            logs.append({a: float(np.log(r[f"tgt_{a}"])) for a in ANCHORS})
        shares = [target_distance_geometry(g)["share_lo"] for g in logs]
        ratios = [target_distance_geometry(g)["R_ff_pp"] for g in logs]
        bags["med_R_ff_pp"].append(float(np.median(ratios)))
        bags["med_share_lo"].append(float(np.median(shares)))
        # Use sample-level corpus geom (not nested LOO) so bootstrap stays O(n_boot·n).
        for hold in ANCHORS:
            h = holdout_anchor_diagnostics(
                sample, hold=hold, method="production_aligned", leave_one_out_corpus=False
            )
            rel = (h.get("rel_err") or {}).get("mae")
            logm = (h.get("abs_log_err") or {}).get("mae")
            bags[f"hold_{hold}_rel_mae"].append(float(rel) if rel is not None else np.nan)
            if hold == "mf":
                bags["hold_mf_log_mae"].append(float(logm) if logm is not None else np.nan)

    def ci(arr: list[float]) -> dict[str, float]:
        a = np.asarray(arr, dtype=float)
        a = a[np.isfinite(a)]
        if a.size == 0:
            return {}
        return {
            "mean": float(np.mean(a)),
            "p025": float(np.quantile(a, 0.025)),
            "p50": float(np.quantile(a, 0.5)),
            "p975": float(np.quantile(a, 0.975)),
        }

    return {
        "n_boot": n_boot,
        "seed": seed,
        "n_rows": n,
        "ci": {k: ci(v) for k, v in bags.items()},
    }


def outer_step_sensitivity_bands(
    tgt_anchors: dict[str, float],
    *,
    corpus_geom: dict[str, float] | None = None,
    delta: float = OUTER_SENSITIVITY,
    outer_taper_r: float = OUTER_TAPER_R,
) -> dict[str, tuple[float, float, float]]:
    """
    Deterministic ±delta sensitivity on outer log-steps (NOT a probabilistic CI).
    Returns dynamic -> (point, lo, hi) in linear metric space.
    Uses the same tapered cumulative offsets as place_equal_log_ladder.
    """
    tgt_log = {a: float(np.log(tgt_anchors[a])) for a in ANCHORS}
    pred_log, audit, _ = place_equal_log_ladder(
        tgt_log, corpus_geom=corpus_geom, outer_taper_r=outer_taper_r
    )
    step_lo = float(audit["step_lo_log"])
    step_hi = float(audit["step_hi_log"])
    r = float(outer_taper_r)
    out: dict[str, tuple[float, float, float]] = {}
    for name, center, step, k in (
        ("ppp", tgt_log["pp"], step_lo, 1),
        ("pppp", tgt_log["pp"], step_lo, 2),
        ("fff", tgt_log["ff"], step_hi, 1),
        ("ffff", tgt_log["ff"], step_hi, 2),
    ):
        off_a = tapered_outer_cum_offset(step * (1.0 - delta), k, r)
        off_b = tapered_outer_cum_offset(step * (1.0 + delta), k, r)
        if name in ("ppp", "pppp"):
            lo_l, hi_l = center - off_b, center - off_a
        else:
            lo_l, hi_l = center + off_a, center + off_b
        lo_l, hi_l = min(lo_l, hi_l), max(lo_l, hi_l)
        out[name] = (float(np.exp(pred_log[name])), float(np.exp(lo_l)), float(np.exp(hi_l)))
    return out


def outer_sensitivity_table(
    results: list[NoteResult],
    df: pd.DataFrame | None = None,
    *,
    delta: float = OUTER_SENSITIVITY,
    taper_rs: Sequence[float] = OUTER_TAPER_SENSITIVITY_RS,
) -> pd.DataFrame:
    """
    Separate sensitivity analysis for outers (±delta on step size) plus
    columns for outer taper r ∈ taper_rs so the taper choice is visibly bounded.
    """
    corpus_geom = corpus_target_distance_summary(df) if df is not None else None
    rows = []
    for res in results:
        if res.status != "ok":
            continue
        bands = outer_step_sensitivity_bands(
            res.target_measured, corpus_geom=corpus_geom, delta=delta
        )
        tgt_log = {a: float(np.log(res.target_measured[a])) for a in ANCHORS}
        taper_preds: dict[float, dict[str, float]] = {}
        for r in taper_rs:
            plog, _, _ = place_equal_log_ladder(
                tgt_log, corpus_geom=corpus_geom, outer_taper_r=float(r)
            )
            taper_preds[float(r)] = {d: float(np.exp(plog[d])) for d in EXTRAPOLATED_BEYOND}
        for d in EXTRAPOLATED_BEYOND:
            point, lo, hi = bands[d]
            row = {
                "note": res.note_target or res.note,
                "dynamic": d,
                "pred": point,
                "sens_lo": lo,
                "sens_hi": hi,
                "rel_halfwidth": abs(hi - lo) / (2.0 * point) if point else np.nan,
                "sensitivity": delta,
                "band_type": "outer_step_sensitivity_not_CI",
                "claim_strength": "weak_extrapolation",
                "outer_taper_r_default": OUTER_TAPER_R,
            }
            for r in taper_rs:
                key = f"pred_r{str(r).replace('.', '')}"
                # 0.7 → pred_r07; 1.0 → pred_r10
                if r == 1.0:
                    key = "pred_r1.0"
                elif r == 0.7:
                    key = "pred_r0.7"
                elif r == 0.8:
                    key = "pred_r0.8"
                elif r == 0.9:
                    key = "pred_r0.9"
                else:
                    key = f"pred_r{r}"
                row[key] = taper_preds[float(r)][d]
            rows.append(row)
    return pd.DataFrame(rows)


def results_to_dataframe(results: list[NoteResult]) -> pd.DataFrame:
    rows = []
    for res in results:
        row = {
            "note": res.note,
            "note_target": res.note_target,
            "status": res.status,
            "quality": res.quality,
            "interval_kind": res.interval_kind,
            "flags": "; ".join(res.flags),
            "warnings": "; ".join(res.warnings),
            "acoustics_rules": "; ".join(res.acoustics_rules_applied),
            "acoustics_notes": "; ".join(res.acoustics_notes),
            "edge_filled_anchor": bool(
                EDGE_FILLED_FLAG in (res.flags or [])
                or float((res.audit or {}).get("edge_filled_anchor", 0.0) or 0.0) >= 0.5
            ),
        }
        for d in DYN_ORDER:
            row[f"pred_{d}"] = res.target_pred.get(d)
            row[f"pred_lo_{d}"] = res.pred_lo.get(d)
            row[f"pred_hi_{d}"] = res.pred_hi.get(d)
            row[f"value_kind_{d}"] = res.value_kind.get(d, "")
            row[f"acoustics_{d}"] = (res.target_pred_acoustics or {}).get(d)
            row[f"tanh_{d}"] = (res.target_pred_tanh or {}).get(d)
        for a in ANCHORS:
            row[f"measured_{a}"] = res.target_measured.get(a)
        for k, v in (res.audit or {}).items():
            row[k] = v
        rows.append(row)
    return pd.DataFrame(rows)


def _acoustics_literature_df() -> pd.DataFrame:
    return pd.DataFrame(ACOUSTICS_LITERATURE_RULES)


def _limitations_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "topic": "anchors",
                "statement": "Only pp, mf, ff are measured IOWA+ORCHIDEA values; never treat other cells as measured.",
            },
            {
                "topic": "intermediates",
                "statement": "p, mp, f are model-derived (equal-log fractions; optional PCHIP on 3 anchors).",
            },
            {
                "topic": "outers",
                "statement": "pppp, ppp, fff, ffff are extrapolated via tapered segment log-steps (step·r^(k−1), default r=0.80; r=1.0 = v1.4). Weakest claim. See Sensitivity_outer (±20% steps and r∈{0.7,0.8,0.9,1.0}) separately from model CIs.",
            },
            {
                "topic": "tanh_track",
                "statement": (
                    "Results_tanh is a THIRD track (ladder_mode=tanh_saturating): "
                    "log y = a + b·tanh(c·(idx−idx0)) when spans are same-sign and not "
                    "one-sided near-flat. N/A reasons: 'non-monotonic anchors' | "
                    "'near-flat segment (|span| < tol)' (tol=TANH_NEAR_FLAT_TOL). "
                    "Run_meta: n_tanh_na_nonmono ≡ n_nonmonotonic_anchors; n_tanh_na_flat; "
                    "n_tanh_na = sum. Never the default."
                ),
            },
            {
                "topic": "intervals",
                "statement": "pred_lo/pred_hi are conjugate EB posterior pushforward bands: θ=(D_lo,D_hi)~N(μ_r,Σ_within), y|θ~N(θ,Σ_meas); hyperparameters cov_scale/meas_ratio calibrated to nominal LOO coverage. Measured anchors kept exact. Not acoustic lab error.",
            },
            {
                "topic": "metric",
                "statement": (
                    "EWSD-like spectral-density / CDM score ≠ SPL and ≠ spectral centroid. "
                    "Empirically non-monotone in dynamics for bowed strings (cello corpus: "
                    "33/49 notes with ff<mf; median R_ff/mf ≈ 0.94). Soft→loud monotonicity "
                    "is a hygiene rule for broken model extrapolation on imputed/extrapolated "
                    "cells only — not a physical law, and never a reason to overwrite measured pp/mf/ff."
                ),
            },
            {
                "topic": "holdout",
                "statement": "Anchor hold-outs test geometric consistency under missing-anchor assumptions, not acoustic truth of intermediates.",
            },
            {
                "topic": "philharmonia",
                "statement": "Philharmonia is not used (removed from code path).",
            },
            {
                "topic": "acoustics",
                "statement": "No bow-force / β / velocity / centroid in this workbook — not a Schelleng/Schoonderwaldt experiment.",
            },
            {
                "topic": "acoustics_prior_sheet",
                "statement": "Results_acoustics_prior is a literature-conditioned SECOND TRACK grounded primarily in Rossing (ed.) The Science of String Instruments, plus Schoonderwaldt/Schelleng/Meyer/Cremer/Benade/Chaigne (R0–R7). It does not overwrite measured pp/mf/ff. Prefer Results for data-faithful imputation; use Results_acoustics_prior only when a force→brightness soft prior is desired.",
            },
        ]
    )


def _literature_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "role": "missing_data_framework",
                "title": "Statistical Analysis with Missing Data",
                "authors": "Roderick J. A. Little; Donald B. Rubin",
                "year": 2019,
                "edition_note": "3rd ed.",
            },
            {
                "role": "log_multiplicative_models",
                "title": "Data Analysis Using Regression and Multilevel/Hierarchical Models",
                "authors": "Andrew Gelman; Jennifer Hill",
                "year": 2007,
                "edition_note": "",
            },
            {
                "role": "ratio_relative_structure",
                "title": "Sampling Techniques",
                "authors": "William G. Cochran",
                "year": 1977,
                "edition_note": "3rd ed. (ratio estimation)",
            },
            {
                "role": "shape_preserving_interpolation",
                "title": "Monotone Piecewise Cubic Interpolation",
                "authors": "F. N. Fritsch; R. E. Carlson",
                "year": 1980,
                "edition_note": "SIAM J. Numer. Anal. 17(2), 238–246",
            },
            {
                "role": "imputation_diagnostics",
                "title": "Flexible Imputation of Missing Data",
                "authors": "Stef van Buuren",
                "year": 2018,
                "edition_note": "2nd ed.",
            },
            {
                "role": "uncertainty_bootstrap",
                "title": "An Introduction to the Bootstrap",
                "authors": "Bradley Efron; Robert J. Tibshirani",
                "year": 1993,
                "edition_note": "",
            },
            {
                "role": "multilevel_partial_pooling",
                "title": "Data Analysis Using Regression and Multilevel/Hierarchical Models",
                "authors": "Andrew Gelman; Jennifer Hill",
                "year": 2007,
                "edition_note": "register-pooled span means",
            },
        ]
    )


def build_run_meta(
    *,
    source: str,
    df: pd.DataFrame,
    results: list[NoteResult],
    use_pchip: bool,
    holdouts: list[dict],
    bootstrap: dict,
    loo: dict,
    span_model: LogSpanModel | None = None,
    calibration: dict | None = None,
    extra: dict | None = None,
) -> dict:
    primary = [h for h in holdouts if h.get("method") == "production_aligned"]
    meta = {
        "method": "iowa_orchidea_equal_log",
        "version": __version__,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "collections": "IOWA+ORCHIDEA only (Philharmonia removed)",
        "n_rows": int(len(df)),
        "n_ok": int(sum(1 for r in results if r.status == "ok")),
        "use_pchip_default_false_cli": True,
        "use_pchip_default_true_gui": True,
        "use_pchip": use_pchip,
        "interval_kind": INTERVAL_KIND,
        "interval_alpha": PRED_INTERVAL_ALPHA,
        "n_pushforward_draws": N_PUSHFORWARD_DRAWS,
        "outer_sensitivity_not_CI": OUTER_SENSITIVITY,
        "outer_taper_r_default": OUTER_TAPER_R,
        "outer_taper_sensitivity_rs": list(OUTER_TAPER_SENSITIVITY_RS),
        "ladder_modes": [LADDER_MODE_EQUAL_LOG, "acoustics_prior", LADDER_MODE_TANH],
        "span_model": span_model.summary() if span_model is not None else {},
        "span_model_calibration_loo": calibration or {},
        "corpus_target_distances": corpus_target_distance_summary(df),
        "anchors_measured": list(ANCHORS),
        "interpolated_inside": list(INTERPOLATED),
        "extrapolated_beyond": list(EXTRAPOLATED_BEYOND),
        "dyn_order": list(DYN_ORDER),
        "holdout_primary_production_aligned": {
            h["hold"]: {k: h[k] for k in ("abs_err", "rel_err", "abs_log_err")} for h in primary
        },
        "holdout_all_methods": {
            f"{h['hold']}|{h['method']}": (h.get("rel_err") or {}).get("mae") for h in holdouts
        },
        "bootstrap": bootstrap,
        "leave_one_note_out": loo,
        "mean_quality": float(np.nanmean([r.quality for r in results if r.status == "ok"])),
        "n_nonmonotonic_anchors": int(
            sum(1 for r in results if r.status == "ok" and "nonmonotonic_anchors" in r.flags)
        ),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    n_tanh_na_nonmono = int(
        sum(
            1
            for r in results
            if r.status == "ok"
            and (
                (r.tanh_fit or {}).get("tanh_na_nonmono", 0.0) == 1.0
                or TANH_NA_REASON_NONMONO in (r.warnings or [])
            )
        )
    )
    n_tanh_na_flat = int(
        sum(
            1
            for r in results
            if r.status == "ok"
            and (
                (r.tanh_fit or {}).get("tanh_na_flat", 0.0) == 1.0
                or TANH_NA_REASON_FLAT in (r.warnings or [])
            )
        )
    )
    n_tanh_na_other = int(
        sum(
            1
            for r in results
            if r.status == "ok"
            and (r.tanh_fit or {}).get("tanh_na", 0.0) == 1.0
            and (r.tanh_fit or {}).get("tanh_na_nonmono", 0.0) != 1.0
            and (r.tanh_fit or {}).get("tanh_na_flat", 0.0) != 1.0
        )
    )
    meta["n_tanh_na_nonmono"] = n_tanh_na_nonmono
    meta["n_tanh_na_flat"] = n_tanh_na_flat
    meta["n_tanh_na"] = n_tanh_na_nonmono + n_tanh_na_flat + n_tanh_na_other
    meta["tanh_near_flat_tol"] = float(TANH_NEAR_FLAT_TOL)
    # Reconcile: strict-opposite tanh refusals ≡ quality nonmonotonic_anchors flag
    if meta["n_tanh_na_nonmono"] != meta["n_nonmonotonic_anchors"]:
        raise AssertionError(
            "n_tanh_na_nonmono (%d) != n_nonmonotonic_anchors (%d); "
            "tanh guard and quality flag must not drift"
            % (meta["n_tanh_na_nonmono"], meta["n_nonmonotonic_anchors"])
        )
    src = Path(source)
    if src.is_file():
        meta["source_sha256"] = file_sha256(src)
        meta["source_mtime"] = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat()
    if extra:
        meta.update(extra)
    return meta


def _style_fill(hex_rgb: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_rgb)


def _apply_results_highlighting(workbook) -> None:
    """Colour-code the sheets/columns the user should actually use."""
    fill_guide_title = _style_fill("1F4E79")
    fill_guide_box = _style_fill("D6EAF8")
    fill_use = _style_fill("C6EFCE")  # green — use these
    fill_anchor = _style_fill("FFF2CC")  # yellow — measured
    fill_interp = _style_fill("DDEBF7")  # blue — interpolated
    fill_extrap = _style_fill("FCE4D6")  # orange — extrapolated
    fill_meta = _style_fill("E7E6E6")  # grey — ids/status
    fill_ci = _style_fill("F2F2F2")  # light grey — uncertainty extras
    font_white = Font(bold=True, color="FFFFFF", size=14)
    font_header = Font(bold=True)

    # --- START_HERE guide ---
    if "START_HERE" in workbook.sheetnames:
        ws = workbook["START_HERE"]
        ws.sheet_properties.tabColor = "1F4E79"
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30), max_col=3):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        # Title row (headers from DataFrame)
        for cell in ws[1]:
            cell.font = font_white
            cell.fill = fill_guide_title
        for r in range(2, ws.max_row + 1):
            for c in range(1, 4):
                ws.cell(r, c).fill = fill_guide_box
        # Emphasize the "Primary sheet" / USE THIS rows
        for r in range(2, ws.max_row + 1):
            item = str(ws.cell(r, 1).value or "").lower()
            if item.startswith("primary") or item.startswith("use this"):
                for c in range(1, 4):
                    ws.cell(r, c).fill = fill_use
                    ws.cell(r, c).font = font_header
            elif "literature second" in item or "acoustics rule" in item:
                for c in range(1, 4):
                    ws.cell(r, c).fill = _style_fill("E2D5F1")  # light purple
            elif "tanh" in item or "third track" in item:
                for c in range(1, 4):
                    ws.cell(r, c).fill = _style_fill("F8CBAD")  # light terracotta
        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 56
        ws.column_dimensions["C"].width = 48

    def _paint_dyn_sheet(ws, *, tab_rgb: str) -> None:
        ws.sheet_properties.tabColor = tab_rgb
        headers = {cell.value: cell.column for cell in ws[1] if cell.value}
        for cell in ws[1]:
            cell.font = font_header
            name = str(cell.value or "")
            if name in ("note", "note_target", "status", "acoustics_rules", "acoustics_notes"):
                cell.fill = fill_meta
            elif name in ANCHORS:
                cell.fill = fill_anchor
            elif name in INTERPOLATED:
                cell.fill = fill_interp
            elif name in EXTRAPOLATED_BEYOND:
                cell.fill = fill_extrap
            else:
                cell.fill = fill_use
        for col_name, fill in (
            *[(a, fill_anchor) for a in ANCHORS],
            *[(d, fill_interp) for d in INTERPOLATED],
            *[(d, fill_extrap) for d in EXTRAPOLATED_BEYOND],
        ):
            if col_name not in headers:
                continue
            col_idx = headers[col_name]
            for r in range(2, ws.max_row + 1):
                ws.cell(r, col_idx).fill = fill
        ws.freeze_panes = "D2"
        for col_idx in range(1, ws.max_column + 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 12
        ws.column_dimensions["A"].width = 10
        ws.column_dimensions["B"].width = 12

    # --- Results (primary, data-faithful) ---
    if "Results" in workbook.sheetnames:
        _paint_dyn_sheet(workbook["Results"], tab_rgb="00B050")

    # --- Results_acoustics_prior (literature second track) ---
    if "Results_acoustics_prior" in workbook.sheetnames:
        _paint_dyn_sheet(workbook["Results_acoustics_prior"], tab_rgb="7030A0")  # purple
        ws = workbook["Results_acoustics_prior"]
        if "acoustics_notes" in {c.value for c in ws[1]}:
            ws.column_dimensions["O"].width = 48

    # --- Results_tanh (third track; never default) ---
    if "Results_tanh" in workbook.sheetnames:
        _paint_dyn_sheet(workbook["Results_tanh"], tab_rgb="C65911")

    if "Acoustics_prior_rules" in workbook.sheetnames:
        ws = workbook["Acoustics_prior_rules"]
        ws.sheet_properties.tabColor = "7030A0"
        for cell in ws[1]:
            cell.font = font_header
            cell.fill = fill_guide_title
            cell.font = font_white
        for col in ws.columns:
            letter = get_column_letter(col[0].column)
            ws.column_dimensions[letter].width = 28 if letter != "D" else 56
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=ws.max_column):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    # --- Predictions_10dyn: highlight main dyn cols; grey CI/kind ---
    if "Predictions_10dyn" in workbook.sheetnames:
        ws = workbook["Predictions_10dyn"]
        ws.sheet_properties.tabColor = "5B9BD5"
        headers = {cell.value: cell.column for cell in ws[1] if cell.value}
        for cell in ws[1]:
            cell.font = font_header
            name = str(cell.value or "")
            if name in DYN_ORDER:
                if name in ANCHORS:
                    cell.fill = fill_anchor
                elif name in INTERPOLATED:
                    cell.fill = fill_interp
                else:
                    cell.fill = fill_extrap
            elif name.endswith("_lo") or name.endswith("_hi") or name.startswith("kind_"):
                cell.fill = fill_ci
                cell.font = Font(bold=True, color="808080")
            else:
                cell.fill = fill_meta
        for name in DYN_ORDER:
            if name not in headers:
                continue
            col_idx = headers[name]
            fill = (
                fill_anchor
                if name in ANCHORS
                else fill_interp
                if name in INTERPOLATED
                else fill_extrap
            )
            for r in range(2, ws.max_row + 1):
                ws.cell(r, col_idx).fill = fill
        ws.freeze_panes = "C2"


def export_excel(
    results: list[NoteResult],
    holdouts: list[dict],
    out_path: Path,
    meta: dict,
    *,
    sensitivity_df: pd.DataFrame | None = None,
    calibration: dict | None = None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pred_df = results_to_dataframe(results)

    # Clean everyday sheet: values + per-cell value_kind (SDA import / hygiene)
    results_df = pred_df[["note", "note_target", "status"]].copy()
    if "edge_filled_anchor" in pred_df.columns:
        results_df["edge_filled_anchor"] = pred_df["edge_filled_anchor"]
    for d in DYN_ORDER:
        results_df[d] = pred_df[f"pred_{d}"]
        results_df[f"kind_{d}"] = pred_df[f"value_kind_{d}"]

    wide_cols = ["note", "note_target", "status", "quality", "flags", "edge_filled_anchor"]
    for d in DYN_ORDER:
        wide_cols += [f"pred_{d}", f"pred_lo_{d}", f"pred_hi_{d}", f"value_kind_{d}"]
    wide = pred_df[[c for c in wide_cols if c in pred_df.columns]].copy()
    rename = {}
    for d in DYN_ORDER:
        rename[f"pred_{d}"] = d
        rename[f"pred_lo_{d}"] = f"{d}_lo"
        rename[f"pred_hi_{d}"] = f"{d}_hi"
        rename[f"value_kind_{d}"] = f"kind_{d}"
    wide = wide.rename(columns=rename)

    mcols = ["note", "note_target", "status", "quality"]
    if "edge_filled_anchor" in pred_df.columns:
        mcols.append("edge_filled_anchor")
    measured = pred_df[mcols + [f"measured_{a}" for a in ANCHORS]].copy()
    measured = measured.rename(columns={f"measured_{a}": a for a in ANCHORS})

    interp = pred_df[["note", "note_target", "status"]].copy()
    for d in INTERPOLATED:
        interp[d] = pred_df[f"pred_{d}"]
        interp[f"{d}_lo"] = pred_df[f"pred_lo_{d}"]
        interp[f"{d}_hi"] = pred_df[f"pred_hi_{d}"]
        interp[f"kind_{d}"] = "interpolated"

    extrap = pred_df[["note", "note_target", "status"]].copy()
    for d in EXTRAPOLATED_BEYOND:
        extrap[d] = pred_df[f"pred_{d}"]
        extrap[f"{d}_lo"] = pred_df[f"pred_lo_{d}"]
        extrap[f"{d}_hi"] = pred_df[f"pred_hi_{d}"]
        extrap[f"kind_{d}"] = "extrapolated_beyond"

    hold_frames = [pd.DataFrame(h["rows"]) for h in holdouts if h.get("rows")]
    hold_df = pd.concat(hold_frames, ignore_index=True) if hold_frames else pd.DataFrame()

    summ_rows = []
    for h in holdouts:
        for metric_name in ("abs_err", "rel_err", "abs_log_err"):
            s = h.get(metric_name) or {}
            if s:
                summ_rows.append({"hold": h["hold"], "method": h.get("method"), "metric": metric_name, **s})
    summ_df = pd.DataFrame(summ_rows)

    boot_rows = []
    for k, v in (meta.get("bootstrap") or {}).get("ci", {}).items():
        boot_rows.append({"quantity": k, **v})
    boot_df = pd.DataFrame(boot_rows)

    meta_df = pd.DataFrame(
        [{"key": k, "value": json.dumps(v) if isinstance(v, (dict, list)) else str(v)} for k, v in meta.items()]
    )
    qcols = ["note", "note_target", "status", "quality", "flags", "warnings"]
    if "edge_filled_anchor" in pred_df.columns:
        qcols.insert(4, "edge_filled_anchor")
    quality_df = pred_df[[c for c in qcols if c in pred_df.columns]].sort_values("quality")

    sens = sensitivity_df if sensitivity_df is not None else outer_sensitivity_table(results)

    cal = calibration or meta.get("span_model_calibration_loo") or {}
    cal_df = pd.DataFrame(
        [{"key": k, "value": json.dumps(v) if isinstance(v, (dict, list)) else str(v)} for k, v in cal.items()]
    )

    # Literature second track (purple): same anchors, regularized imputed/outers
    ac_df = pred_df[["note", "note_target", "status"]].copy()
    for d in DYN_ORDER:
        ac_df[d] = pred_df[f"acoustics_{d}"]
    ac_df["acoustics_rules"] = pred_df["acoustics_rules"]
    ac_df["acoustics_notes"] = pred_df["acoustics_notes"]

    # Third track (terracotta): tanh_saturating (monotone-anchor notes only; N/A → blank)
    # Re-check counters at export so they cannot drift from build_run_meta
    if "n_tanh_na_nonmono" in meta and "n_nonmonotonic_anchors" in meta:
        if int(meta["n_tanh_na_nonmono"]) != int(meta["n_nonmonotonic_anchors"]):
            raise AssertionError(
                "export: n_tanh_na_nonmono (%s) != n_nonmonotonic_anchors (%s)"
                % (meta["n_tanh_na_nonmono"], meta["n_nonmonotonic_anchors"])
            )
    tanh_df = pred_df[["note", "note_target", "status"]].copy()
    for d in DYN_ORDER:
        tanh_df[d] = pred_df[f"tanh_{d}"]
    tanh_notes: list[str] = []
    for res in results:
        note = ""
        if res.status == "ok":
            for w in res.warnings or []:
                if w in (TANH_NA_REASON_NONMONO, TANH_NA_REASON_FLAT):
                    note = f"saturating model inapplicable: {w}"
                    break
                if isinstance(w, str) and w.startswith("tanh_na_"):
                    note = f"saturating model inapplicable: {w}"
                    break
        tanh_notes.append(note)
    # Pad if results/pred_df length mismatch (skipped rows already in pred_df)
    while len(tanh_notes) < len(tanh_df):
        tanh_notes.append("")
    tanh_df["tanh_note"] = tanh_notes[: len(tanh_df)]

    pchip_on = bool(meta.get("use_pchip", False))
    interior_geom = "PCHIP on 3 anchors" if pchip_on else "equal-log fractions (p⅓, mp⅔, f½)"
    n_tanh_na = int(meta.get("n_tanh_na", 0) or 0)
    n_tanh_na_nonmono = int(meta.get("n_tanh_na_nonmono", 0) or 0)
    n_tanh_na_flat = int(meta.get("n_tanh_na_flat", 0) or 0)
    guide = pd.DataFrame(
        [
            {
                "item": "USE THIS WORKBOOK",
                "detail": "Open the green tab: Results",
                "columns_or_notes": "that sheet is enough for everyday use",
            },
            {
                "item": "Primary sheet",
                "detail": "Results (green tab) — data-faithful imputation",
                "columns_or_notes": (
                    f"geometry: interiors={interior_geom}; "
                    f"outers=tapered equal-log (r={OUTER_TAPER_R} default; r=1.0=v1.4); "
                    f"this run use_pchip={pchip_on}"
                ),
            },
            {
                "item": "Main columns on Results",
                "detail": "The 10 dynamic values + kind_* per cell",
                "columns_or_notes": (
                    "pppp…ffff values; kind_* = measured_anchor|interior_filled_anchor|edge_filled_anchor|"
                    "interpolated|extrapolated_beyond; edge_filled_anchor column flags upstream "
                    "extrapol_data --fill-panel edges (SDA import / hygiene — true lab anchors inviolable; "
                    "CDM not soft→loud by law)"
                ),
            },
            {
                "item": "Colour key - yellow",
                "detail": "Measured anchors (library values)",
                "columns_or_notes": "pp, mf, ff",
            },
            {
                "item": "Colour key - blue",
                "detail": "Interpolated (model-derived)",
                "columns_or_notes": "p, mp, f",
            },
            {
                "item": "Colour key - orange",
                "detail": "Extrapolated beyond anchors (weakest)",
                "columns_or_notes": "pppp, ppp, fff, ffff",
            },
            {
                "item": "Literature second track",
                "detail": "Results_acoustics_prior (purple tab)",
                "columns_or_notes": (
                    "geometry: equal-log interiors + literature outer shrink/taper (R0–R7); "
                    "anchors unchanged; primary book Rossing The Science of String Instruments"
                ),
            },
            {
                "item": "Acoustics rule list",
                "detail": "Acoustics_prior_rules (purple tab)",
                "columns_or_notes": "R0–R7; R7=outer taper (Meyer 2009 + Patterson 1974); primary local PDF: Thomas D. Rossing_The Science of String Instruments.pdf",
            },
            {
                "item": "Third track (tanh)",
                "detail": "Results_tanh — ladder_mode=tanh_saturating (never default)",
                "columns_or_notes": (
                    "Covers monotone-anchor notes only by design (same-sign pp→mf and mf→ff). "
                    "N/A reasons: 'non-monotonic anchors' | "
                    f"'near-flat segment (|span| < tol)' with tol={TANH_NEAR_FLAT_TOL:g} (internal_default). "
                    f"this run n_tanh_na={n_tanh_na} "
                    f"(nonmono={n_tanh_na_nonmono} ≡ n_nonmonotonic_anchors, flat={n_tanh_na_flat}). "
                    "geometry: log y = a + b·tanh(c·(idx−idx0)); raw-curve anchor verify ≤1e-6 log"
                ),
            },
            {
                "item": "Geometry by sheet",
                "detail": "Which sheet used which geometry (this run)",
                "columns_or_notes": (
                    f"Results: equal_log + taper_r={OUTER_TAPER_R}, interiors={'PCHIP' if pchip_on else 'equal-log'}; "
                    f"Results_acoustics_prior: literature regularizer + same taper; "
                    f"Results_tanh: tanh_saturating (monotone-anchor notes only); "
                    f"CLI default use_pchip=False; GUI default use_pchip=True"
                ),
            },
            {
                "item": "Optional full table",
                "detail": "Predictions_10dyn (blue tab)",
                "columns_or_notes": "same values + grey *_lo / *_hi uncertainty columns",
            },
            {
                "item": "Do not treat as measured",
                "detail": "Anything except yellow pp / mf / ff",
                "columns_or_notes": "see Limitations sheet",
            },
            {
                "item": "Diagnostics only",
                "detail": "Holdout_*, Bootstrap_CI, Interval_calibration, Sensitivity_outer, Run_meta",
                "columns_or_notes": "Sensitivity_outer includes pred_r0.7..pred_r1.0 taper columns; not needed for everyday use",
            },
        ]
    )

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        guide.to_excel(xw, sheet_name="START_HERE", index=False)
        results_df.to_excel(xw, sheet_name="Results", index=False)
        ac_df.to_excel(xw, sheet_name="Results_acoustics_prior", index=False)
        tanh_df.to_excel(xw, sheet_name="Results_tanh", index=False)
        _acoustics_literature_df().to_excel(xw, sheet_name="Acoustics_prior_rules", index=False)
        wide.to_excel(xw, sheet_name="Predictions_10dyn", index=False)
        measured.to_excel(xw, sheet_name="Measured_anchors", index=False)
        interp.to_excel(xw, sheet_name="Interpolated", index=False)
        extrap.to_excel(xw, sheet_name="Extrapolated", index=False)
        pred_df.to_excel(xw, sheet_name="Predictions_detail", index=False)
        quality_df.to_excel(xw, sheet_name="Quality_flags", index=False)
        summ_df.to_excel(xw, sheet_name="Holdout_summary", index=False)
        if not hold_df.empty:
            hold_df.to_excel(xw, sheet_name="Holdout_rows", index=False)
        if not boot_df.empty:
            boot_df.to_excel(xw, sheet_name="Bootstrap_CI", index=False)
        if not cal_df.empty:
            cal_df.to_excel(xw, sheet_name="Interval_calibration", index=False)
        if sens is not None and not sens.empty:
            sens.to_excel(xw, sheet_name="Sensitivity_outer", index=False)
        _limitations_df().to_excel(xw, sheet_name="Limitations", index=False)
        _literature_df().to_excel(xw, sheet_name="Literature", index=False)
        meta_df.to_excel(xw, sheet_name="Run_meta", index=False)
        _apply_results_highlighting(xw.book)
    return out_path


def _default_panel() -> Path:
    root = Path(r"C:\Users\lmr20\Desktop\Violino - extrapol")
    panel = discover_panel_xlsx(root)
    if panel is None:
        raise SystemExit(
            "No dynamics panel .xlsx found. Expected something like Violino_dinámicas.xlsx "
            f"in {root}"
        )
    return panel


def run_pipeline(
    df: pd.DataFrame,
    *,
    source: str,
    use_pchip: bool = False,
    n_boot: int = BOOTSTRAP_DEFAULT,
    seed: int = RNG_SEED_DEFAULT,
    n_pushforward: int = N_PUSHFORWARD_DRAWS,
) -> tuple[list[NoteResult], list[dict], dict, dict, dict]:
    """Full research pipeline: span model → transfer → holdouts → bootstrap → LOO → meta."""
    span_model = fit_log_span_model(df, calibrate=True)
    calibration = calibrate_log_span_model(df, model=span_model, seed=seed)
    results = run_transfer(
        df,
        use_pchip=use_pchip,
        span_model=span_model,
        n_pushforward=n_pushforward,
        seed=seed,
    )
    holdouts = holdout_all_baselines(df)
    bootstrap = bootstrap_diagnostics(df, n_boot=n_boot, seed=seed, use_pchip=use_pchip)
    loo = leave_one_note_out_register(df, use_pchip=use_pchip)
    meta = build_run_meta(
        source=source,
        df=df,
        results=results,
        use_pchip=use_pchip,
        holdouts=holdouts,
        bootstrap=bootstrap,
        loo=loo,
        span_model=span_model,
        calibration=calibration,
    )
    return results, holdouts, bootstrap, loo, meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="IOWA+ORCHIDEA-only 10-level dynamic imputation (v%s)" % __version__)
    p.add_argument("--panel", type=Path, default=None)
    p.add_argument("--paste-file", type=Path, default=None, help="Text file: note pp mf ff")
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "outputs" / "iowa_orchidea_dynamics.xlsx",
    )
    p.add_argument(
        "--pchip",
        action="store_true",
        help="Optional PCHIP polish on intermediates from the 3 anchors (CLI default: off; GUI default: on)",
    )
    p.add_argument("--no-pchip", action="store_true", help=argparse.SUPPRESS)  # backward compat
    p.add_argument("--n-boot", type=int, default=BOOTSTRAP_DEFAULT)
    p.add_argument("--seed", type=int, default=RNG_SEED_DEFAULT)
    args = p.parse_args(argv)

    if args.paste_file is not None:
        df = parse_paste_panel(Path(args.paste_file).read_text(encoding="utf-8"))
        source = str(args.paste_file)
        if args.out.name == "iowa_orchidea_dynamics.xlsx":
            args.out = args.out.with_name("paste_dynamics.xlsx")
    else:
        panel = args.panel or _default_panel()
        df = load_panel_xlsx(panel)
        source = str(panel)

    use_pchip = bool(args.pchip) and not bool(args.no_pchip)
    results, holdouts, bootstrap, loo, meta = run_pipeline(
        df, source=source, use_pchip=use_pchip, n_boot=args.n_boot, seed=args.seed
    )
    sens = outer_sensitivity_table(results, df)
    out = export_excel(
        results,
        holdouts,
        args.out,
        meta,
        sensitivity_df=sens,
        calibration=meta.get("span_model_calibration_loo"),
    )
    print(f"Wrote {out}")
    print(json.dumps(meta.get("holdout_primary_production_aligned"), indent=2))
    print("span_model:", json.dumps(meta.get("span_model"), indent=2))
    print("calibration:", json.dumps(meta.get("span_model_calibration_loo"), indent=2))
    print("bootstrap_ci_keys:", sorted((bootstrap.get("ci") or {}).keys()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
