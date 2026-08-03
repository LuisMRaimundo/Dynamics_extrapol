#!/usr/bin/env python3
"""GUI: Excel panel + column mapper + optional paste tool."""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from dynamics_predicter.transfer import (
    ColumnMap,
    __version__,
    discover_panel_xlsx,
    excel_col_letter,
    export_excel,
    inspect_workbook,
    load_panel_xlsx,
    outer_sensitivity_table,
    parse_paste_panel,
    run_pipeline,
)

ROOT = Path(__file__).resolve().parent.parent  # package root (Dynamics_predicter/)
DEFAULT_PANEL_DIR = ROOT.parent

PASTE_EXAMPLE = """note\tpp\tmf\tff
G3\t57.39\t61.66\t70.62
A3\t30.70\t29.14\t42.29
C5\t16.40\t16.67\t16.59
"""

# Violino_dinámicas.xlsx IOWA+ORCHIDEA block (0-based)
PRESET_IOWA = ColumnMap(note=0, note_target=8, pp=9, mf=10, ff=11, data_start_row=2)


class ColumnMapDialog(tk.Toplevel):
    """Choose which Excel columns are note / pp / mf / ff."""

    def __init__(self, master: tk.Tk, path: Path, existing: ColumnMap | None = None) -> None:
        super().__init__(master)
        self.title("Map Excel columns")
        self.geometry("720x520")
        self.transient(master)
        self.grab_set()
        self.result: ColumnMap | None = None
        self.path = Path(path)
        self._info: dict[str, Any] | None = None

        self.sheet_var = tk.StringVar()
        self.note_var = tk.StringVar()
        self.note_t_var = tk.StringVar()
        self.pp_var = tk.StringVar()
        self.mf_var = tk.StringVar()
        self.ff_var = tk.StringVar()
        self.start_var = tk.StringVar(value="3")

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text=f"File: {self.path.name}", wraplength=680).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        ttk.Label(frm, text="Sheet").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.sheet_cb = ttk.Combobox(frm, textvariable=self.sheet_var, state="readonly", width=40)
        self.sheet_cb.grid(row=1, column=1, columnspan=2, sticky="we", pady=(10, 0))
        self.sheet_cb.bind("<<ComboboxSelected>>", lambda _e: self._reload_sheet())

        grid = ttk.Frame(frm)
        grid.grid(row=2, column=0, columnspan=3, sticky="we", pady=12)
        self._combos: dict[str, ttk.Combobox] = {}
        fields = [
            ("note", "Note labels", self.note_var),
            ("note_target", "Note target (optional)", self.note_t_var),
            ("pp", "pp (pianissimo)", self.pp_var),
            ("mf", "mf (mezzo-forte)", self.mf_var),
            ("ff", "ff (fortissimo)", self.ff_var),
        ]
        for i, (key, label, var) in enumerate(fields):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=3)
            cb = ttk.Combobox(grid, textvariable=var, state="readonly", width=56)
            cb.grid(row=i, column=1, sticky="we", pady=3, padx=(8, 0))
            self._combos[key] = cb
        grid.columnconfigure(1, weight=1)

        ttk.Label(frm, text="First data row (1-based Excel row)").grid(row=3, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.start_var, width=8).grid(row=3, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(btns, text="Auto-detect", command=self._apply_suggested).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Preset: IOWA+ORCHIDEA (A / I / J / K / L)", command=self._apply_iowa).pack(
            side=tk.LEFT, padx=(0, 6)
        )

        ttk.Label(frm, text="Preview (first rows / first columns)").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        self.preview = tk.Text(frm, height=12, wrap=tk.NONE, font=("Consolas", 9))
        self.preview.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=4)
        frm.rowconfigure(6, weight=1)
        frm.columnconfigure(1, weight=1)

        ok_row = ttk.Frame(frm)
        ok_row.grid(row=7, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(ok_row, text="Cancel", command=self._cancel).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(ok_row, text="Use mapping", command=self._ok).pack(side=tk.RIGHT)

        self._load(existing)

    def _load(self, existing: ColumnMap | None) -> None:
        try:
            sheet = existing.sheet if existing is not None else 0
            self._info = inspect_workbook(self.path, sheet=sheet)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Inspect failed", str(exc), parent=self)
            self.destroy()
            return
        assert self._info is not None
        self.sheet_cb["values"] = self._info["sheets"]
        self.sheet_var.set(self._info["sheet"])
        self._fill_column_choices()
        if existing is not None:
            self._apply_map(existing)
        else:
            self._apply_suggested()
        self._render_preview()

    def _reload_sheet(self) -> None:
        try:
            self._info = inspect_workbook(self.path, sheet=self.sheet_var.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Inspect failed", str(exc), parent=self)
            return
        self._fill_column_choices()
        self._apply_suggested()
        self._render_preview()

    def _fill_column_choices(self) -> None:
        assert self._info is not None
        labels = list(self._info["column_labels"])
        optional = ["(none)"] + labels
        for key, cb in self._combos.items():
            cb["values"] = optional if key == "note_target" else labels

    def _label_for_index(self, idx: int | None) -> str:
        assert self._info is not None
        if idx is None:
            return "(none)"
        labels = self._info["column_labels"]
        if 0 <= idx < len(labels):
            return labels[idx]
        return f"{excel_col_letter(idx)}"

    def _index_from_label(self, label: str) -> int | None:
        if not label or label == "(none)":
            return None
        assert self._info is not None
        labels = self._info["column_labels"]
        if label in labels:
            return labels.index(label)
        # tolerate bare letter
        letter = label.split("—")[0].split("-")[0].strip().upper()
        for i, lab in enumerate(labels):
            if lab.split("—")[0].strip().upper() == letter:
                return i
        raise ValueError(f"Unknown column choice: {label}")

    def _apply_map(self, cmap: ColumnMap) -> None:
        self.note_var.set(self._label_for_index(cmap.note))
        self.note_t_var.set(self._label_for_index(cmap.note_target))
        self.pp_var.set(self._label_for_index(cmap.pp))
        self.mf_var.set(self._label_for_index(cmap.mf))
        self.ff_var.set(self._label_for_index(cmap.ff))
        self.start_var.set(str(int(cmap.data_start_row) + 1))
        if cmap.sheet is not None:
            self.sheet_var.set(str(cmap.sheet))

    def _apply_suggested(self) -> None:
        assert self._info is not None
        sug = self._info.get("suggested")
        if isinstance(sug, ColumnMap):
            self._apply_map(sug)

    def _apply_iowa(self) -> None:
        m = ColumnMap(
            note=PRESET_IOWA.note,
            note_target=PRESET_IOWA.note_target,
            pp=PRESET_IOWA.pp,
            mf=PRESET_IOWA.mf,
            ff=PRESET_IOWA.ff,
            data_start_row=PRESET_IOWA.data_start_row,
            sheet=self.sheet_var.get() or 0,
        )
        self._apply_map(m)

    def _render_preview(self) -> None:
        assert self._info is not None
        self.preview.delete("1.0", tk.END)
        labels = self._info["column_labels"][:12]
        self.preview.insert(tk.END, "row\t" + "\t".join(labels) + "\n")
        for pr in self._info["preview"]:
            vals = ["" if v is None else str(v) for v in pr["values"][:12]]
            self.preview.insert(tk.END, f"{pr['row']}\t" + "\t".join(vals) + "\n")

    def _current_map(self) -> ColumnMap:
        note = self._index_from_label(self.note_var.get())
        pp = self._index_from_label(self.pp_var.get())
        mf = self._index_from_label(self.mf_var.get())
        ff = self._index_from_label(self.ff_var.get())
        note_t = self._index_from_label(self.note_t_var.get())
        if note is None or pp is None or mf is None or ff is None:
            raise ValueError("note, pp, mf, and ff are required.")
        try:
            start_1based = int(self.start_var.get().strip())
        except ValueError as exc:
            raise ValueError("First data row must be an integer.") from exc
        if start_1based < 1:
            raise ValueError("First data row must be ≥ 1.")
        return ColumnMap(
            note=note,
            pp=pp,
            mf=mf,
            ff=ff,
            note_target=note_t,
            data_start_row=start_1based - 1,
            sheet=self.sheet_var.get() or 0,
        )

    def _ok(self) -> None:
        try:
            cmap = self._current_map()
            # validate by loading
            df = load_panel_xlsx(self.path, column_map=cmap)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Invalid mapping", str(exc), parent=self)
            return
        if len(df) == 0:
            messagebox.showerror("Invalid mapping", "Mapping produced 0 rows.", parent=self)
            return
        self.result = cmap
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Dynamics_predicter v{__version__} — IOWA + ORCHIDEA")
        self.geometry("900x700")
        self.use_pchip = tk.BooleanVar(value=False)
        self.n_boot = tk.IntVar(value=200)
        self.panel = tk.StringVar(value=self._guess_panel())
        self.out_excel = tk.StringVar(value=str(ROOT / "outputs" / "iowa_orchidea_dynamics.xlsx"))
        self.out_paste = tk.StringVar(value=str(ROOT / "outputs" / "paste_dynamics.xlsx"))
        self.column_map: ColumnMap | None = None
        self.map_summary = tk.StringVar(value="Columns: (auto-detect)")

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tab_excel = ttk.Frame(nb, padding=10)
        self.tab_paste = ttk.Frame(nb, padding=10)
        nb.add(self.tab_excel, text="Excel panel")
        nb.add(self.tab_paste, text="Paste tool (optional)")
        self._build_excel_tab()
        self._build_paste_tab()
        # Prefill IOWA mapping when the default dinámicas panel is selected
        if self.panel.get():
            self._try_autoload_map(silent=True)

    def _guess_panel(self) -> str:
        panel = discover_panel_xlsx(DEFAULT_PANEL_DIR)
        return str(panel) if panel is not None else ""

    def _browse_out(self, var: tk.StringVar) -> None:
        path = filedialog.asksaveasfilename(
            initialdir=str(ROOT / "outputs"),
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
        )
        if path:
            var.set(path)

    def _export(self, df, out: Path, source: str, log: tk.Text) -> None:
        use_pchip = bool(self.use_pchip.get())
        n_boot = max(50, int(self.n_boot.get()))
        results, holdouts, bootstrap, loo, meta = run_pipeline(
            df, source=source, use_pchip=use_pchip, n_boot=n_boot
        )
        sens = outer_sensitivity_table(results, df)
        export_excel(
            results,
            holdouts,
            out,
            meta,
            sensitivity_df=sens,
            calibration=meta.get("span_model_calibration_loo"),
        )
        log.insert(tk.END, f"OK → {out}\n")
        log.insert(
            tk.END,
            f"rows={meta['n_rows']} ok={meta['n_ok']} mean_quality={meta['mean_quality']:.3f} "
            f"nonmono_anchors={meta['n_nonmonotonic_anchors']}\n",
        )
        log.insert(tk.END, "holdout (production_aligned):\n")
        log.insert(tk.END, json.dumps(meta["holdout_primary_production_aligned"], indent=2) + "\n")
        log.insert(tk.END, f"bootstrap n={bootstrap.get('n_boot')}\n")
        log.insert(tk.END, f"loo: {json.dumps(loo)}\n")
        log.see(tk.END)
        messagebox.showinfo("Done", f"Wrote:\n{out}")

    def _opts(self, frm, row: int) -> int:
        ttk.Checkbutton(
            frm,
            text="Optional PCHIP on intermediates (default off = equal-log)",
            variable=self.use_pchip,
        ).grid(row=row, column=0, sticky="w", pady=(8, 0))
        row += 1
        boot_frm = ttk.Frame(frm)
        boot_frm.grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Label(boot_frm, text="Bootstrap draws").pack(side=tk.LEFT)
        ttk.Spinbox(boot_frm, from_=50, to=2000, textvariable=self.n_boot, width=8).pack(
            side=tk.LEFT, padx=8
        )
        return row + 1

    def _build_excel_tab(self) -> None:
        frm = self.tab_excel
        ttk.Label(frm, text="Panel Excel (IOWA+ORCHIDEA pp / mf / ff)").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.panel, width=72).grid(row=1, column=0, sticky="we", padx=(0, 8))
        ttk.Button(frm, text="Browse…", command=self._browse_panel).grid(row=1, column=1)

        map_row = ttk.Frame(frm)
        map_row.grid(row=2, column=0, columnspan=2, sticky="we", pady=(8, 0))
        ttk.Button(map_row, text="Map columns…", command=self._open_mapper).pack(side=tk.LEFT)
        ttk.Label(map_row, textvariable=self.map_summary).pack(side=tk.LEFT, padx=10)

        ttk.Label(frm, text="Output Excel").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=self.out_excel, width=72).grid(row=4, column=0, sticky="we", padx=(0, 8))
        ttk.Button(frm, text="Browse…", command=lambda: self._browse_out(self.out_excel)).grid(row=4, column=1)
        row = self._opts(frm, 5)
        ttk.Button(frm, text="Run from Excel panel", command=self._run_excel).grid(
            row=row, column=0, sticky="w", pady=10
        )
        self.log_excel = tk.Text(frm, height=14, wrap=tk.WORD)
        self.log_excel.grid(row=row + 1, column=0, columnspan=2, sticky="nsew")
        frm.rowconfigure(row + 1, weight=1)
        frm.columnconfigure(0, weight=1)
        self.log_excel.insert(
            tk.END,
            f"v{__version__}: use Map columns… to pick note / pp / mf / ff "
            "(recommended for Violino_dinámicas.xlsx → preset IOWA+ORCHIDEA).\n"
            "Tip: do not use Zenodo Arco_ordinario workbooks unless you map valid pp/mf/ff columns.\n",
        )

    def _set_map_summary(self) -> None:
        if self.column_map is None:
            self.map_summary.set("Columns: (auto-detect)")
            return
        m = self.column_map
        nt = excel_col_letter(m.note_target) if m.note_target is not None else "—"
        self.map_summary.set(
            f"Columns: note={excel_col_letter(m.note)} target={nt} "
            f"pp={excel_col_letter(m.pp)} mf={excel_col_letter(m.mf)} ff={excel_col_letter(m.ff)} "
            f"start_row={m.data_start_row + 1}"
        )

    def _open_mapper(self) -> None:
        panel = Path(self.panel.get())
        if not panel.is_file():
            messagebox.showerror("Missing panel", f"File not found:\n{panel}")
            return
        dlg = ColumnMapDialog(self, panel, existing=self.column_map)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.column_map = dlg.result
            self._set_map_summary()
            self.log_excel.insert(tk.END, f"Column map set: {self.map_summary.get()}\n")
            self.log_excel.see(tk.END)

    def _try_autoload_map(self, *, silent: bool = False) -> None:
        panel = Path(self.panel.get())
        if not panel.is_file():
            return
        try:
            info = inspect_workbook(panel)
            sug = info.get("suggested")
            if isinstance(sug, ColumnMap):
                self.column_map = sug
                self._set_map_summary()
                if not silent:
                    self.log_excel.insert(tk.END, f"Auto-mapped: {self.map_summary.get()}\n")
        except Exception as exc:  # noqa: BLE001
            if not silent:
                self.log_excel.insert(tk.END, f"Auto-map failed: {exc}\n")

    def _browse_panel(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=str(DEFAULT_PANEL_DIR), filetypes=[("Excel", "*.xlsx")]
        )
        if path:
            self.panel.set(path)
            self.column_map = None
            self._set_map_summary()
            # Open mapper immediately so the user chooses columns
            self._open_mapper()

    def _run_excel(self) -> None:
        panel = Path(self.panel.get())
        if not panel.is_file():
            messagebox.showerror("Missing panel", f"File not found:\n{panel}")
            return
        try:
            if self.column_map is None:
                # Offer mapper instead of failing obscurely
                if messagebox.askyesno(
                    "Map columns?",
                    "No column map is set. Open the column mapper now?\n\n"
                    "Choose IOWA+ORCHIDEA columns (pp / mf / ff), not Philharmonia.",
                ):
                    self._open_mapper()
                    if self.column_map is None:
                        return
                else:
                    df = load_panel_xlsx(panel)
                    self._export(df, Path(self.out_excel.get()), str(panel), self.log_excel)
                    return
            df = load_panel_xlsx(panel, column_map=self.column_map)
            self.log_excel.insert(tk.END, f"Loaded {len(df)} rows with {self.map_summary.get()}\n")
            self._export(df, Path(self.out_excel.get()), str(panel), self.log_excel)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", str(exc))
            self.log_excel.insert(tk.END, f"ERROR: {exc}\n")
            if messagebox.askyesno("Map columns?", "Open the column mapper to fix this?"):
                self._open_mapper()

    def _build_paste_tab(self) -> None:
        frm = self.tab_paste
        ttk.Label(frm, text="Paste note + pp + mf + ff (tab / CSV / spaces). Header optional.").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.paste_box = tk.Text(frm, height=14, wrap=tk.NONE, font=("Consolas", 10))
        self.paste_box.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=6)
        self.paste_box.insert("1.0", PASTE_EXAMPLE)
        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, sticky="w")
        ttk.Button(btns, text="Clear", command=lambda: self.paste_box.delete("1.0", tk.END)).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btns, text="Load example", command=self._load_example).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Paste from clipboard", command=self._paste_clipboard).pack(side=tk.LEFT)
        ttk.Label(frm, text="Output Excel").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=self.out_paste, width=72).grid(row=4, column=0, sticky="we", padx=(0, 8))
        ttk.Button(frm, text="Browse…", command=lambda: self._browse_out(self.out_paste)).grid(row=4, column=1)
        row = self._opts(frm, 5)
        ttk.Button(frm, text="Run from paste", command=self._run_paste).grid(row=row, column=0, sticky="w", pady=10)
        self.log_paste = tk.Text(frm, height=8, wrap=tk.WORD)
        self.log_paste.grid(row=row + 1, column=0, columnspan=2, sticky="nsew")
        frm.rowconfigure(1, weight=3)
        frm.rowconfigure(row + 1, weight=1)
        frm.columnconfigure(0, weight=1)

    def _load_example(self) -> None:
        self.paste_box.delete("1.0", tk.END)
        self.paste_box.insert("1.0", PASTE_EXAMPLE)

    def _paste_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("Clipboard", "Clipboard is empty or not text.")
            return
        self.paste_box.delete("1.0", tk.END)
        self.paste_box.insert("1.0", text)

    def _run_paste(self) -> None:
        try:
            df = parse_paste_panel(self.paste_box.get("1.0", tk.END))
            self._export(df, Path(self.out_paste.get()), "paste", self.log_paste)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", str(exc))
            self.log_paste.insert(tk.END, f"ERROR: {exc}\n")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
