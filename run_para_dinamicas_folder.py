#!/usr/bin/env python3
"""Run Dynamics_predicter (same defaults as start.bat / GUI) on every Zenodo book
in C:\\Users\\lmr20\\Desktop\\para dinâmicas.

Two Media layouts are accepted:
  - compact block: note + cross-collection mean pp/mf/ff  (e.g. Basson_Media)
  - compact block: note + Media pp/mf/ff                   (e.g. DBass_Media)

Results stay in that folder as <stem>_Dynamics10.xlsx. Source books are not overwritten.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run_all_instruments_batch import (  # noqa: E402
    column_map_for_media,
    reconstruct_media_from_dyn_sheets,
)
from dynamics_predicter.transfer import (  # noqa: E402
    __version__,
    export_excel,
    load_panel_xlsx,
    outer_sensitivity_table,
    run_pipeline,
)

FOLDER = Path(r"C:\Users\lmr20\Desktop")
N_BOOT = 200  # GUI / start.bat default


def find_folder() -> Path:
    hits = [p for p in FOLDER.iterdir() if p.is_dir() and p.name.lower().startswith("para din")]
    if not hits:
        raise SystemExit(f"No 'para dinâmicas' folder under {FOLDER}")
    return hits[0]


def source_files(folder: Path) -> list[Path]:
    out = []
    for p in sorted(folder.glob("*.xlsx")):
        if p.name.startswith("~$"):
            continue
        if p.name.endswith("_Dynamics10.xlsx"):
            continue
        if "batch_log" in p.name.lower():
            continue
        out.append(p)
    return out


def run_one(src: Path, out: Path) -> dict:
    rec: dict = {"source": src.name, "out": out.name}
    sheet, cmap = column_map_for_media(src)
    rec["sheet"] = sheet
    rec["cols"] = cmap.to_dict()
    rec["layout"] = (
        "cross-collection_mean"
        if any(
            "cross" in str(x).lower()
            for x in (cmap.to_dict(),)
        )
        else "media_ppmf_ff"
    )
    try:
        df = load_panel_xlsx(src, column_map=cmap)
        rec["source_mode"] = "media_sheet"
    except ValueError:
        df = reconstruct_media_from_dyn_sheets(src)
        rec["source_mode"] = "reconstructed_dyn_sheets"
        rec["reconstruct_sheets"] = df.attrs.get("reconstruct_sheets")
    n_complete = int(df["complete_anchors"].sum()) if "complete_anchors" in df.columns else len(df)
    rec["n_rows"] = int(len(df))
    rec["n_complete"] = n_complete
    if n_complete == 0:
        df = reconstruct_media_from_dyn_sheets(src)
        rec["source_mode"] = "reconstructed_dyn_sheets"
        rec["reconstruct_sheets"] = df.attrs.get("reconstruct_sheets")
        n_complete = int(df["complete_anchors"].sum())
        rec["n_rows"] = int(len(df))
        rec["n_complete"] = n_complete
    if n_complete == 0:
        rec["ok"] = False
        rec["error"] = "0 complete pp/mf/ff rows"
        return rec
    work = df.loc[df["complete_anchors"]].copy() if "complete_anchors" in df.columns else df
    results, holdouts, _bootstrap, _loo, meta = run_pipeline(
        work,
        source=str(src),
        use_pchip=True,
        n_boot=N_BOOT,
    )
    sens = outer_sensitivity_table(results, work)
    export_excel(
        results,
        holdouts,
        out,
        meta,
        sensitivity_df=sens,
        calibration=meta.get("span_model_calibration_loo"),
    )
    rec["ok"] = True
    rec["n_ok"] = meta.get("n_ok")
    rec["mean_quality"] = meta.get("mean_quality")
    rec["n_nonmonotonic_anchors"] = meta.get("n_nonmonotonic_anchors")
    rec["version"] = __version__
    return rec


def main() -> int:
    folder = find_folder()
    files = source_files(folder)
    print(f"Folder: {folder}")
    print(f"Sources: {len(files)}  pipeline v{__version__}  pchip=True  n_boot={N_BOOT}")
    summaries = []
    n_ok = 0
    for i, src in enumerate(files, 1):
        out = folder / f"{src.stem}_Dynamics10.xlsx"
        print(f"[{i}/{len(files)}] {src.name} …", flush=True)
        try:
            rec = run_one(src, out)
        except Exception as exc:  # noqa: BLE001
            rec = {
                "source": src.name,
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        summaries.append(rec)
        if rec.get("ok"):
            n_ok += 1
            print(
                f"  OK n={rec.get('n_complete')} sheet={rec.get('sheet')} -> {rec.get('out')}",
                flush=True,
            )
        else:
            print(f"  FAIL {rec.get('error')}", flush=True)
    log = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "folder": str(folder),
        "version": __version__,
        "n_boot": N_BOOT,
        "use_pchip": True,
        "n_ok": n_ok,
        "n_files": len(files),
        "jobs": summaries,
    }
    log_path = folder / "Dynamics10_batch_log.json"
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    print(f"Done {n_ok}/{len(files)}. Log: {log_path}")
    return 0 if n_ok == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
