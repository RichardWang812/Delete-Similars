#!/usr/bin/env python3
from __future__ import annotations

import queue
import shutil
import threading
import tkinter as tk
from dataclasses import asdict
import datetime as dt
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from find_similar_old_files import (
    ScanStats,
    build_deletion_groups,
    delete_path,
    fmt_mtime,
    scan_fingerprints,
    suggest_similar_groups,
)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("相似文件清理（交互确认）")
        self.minsize(980, 640)

        self._work_q: queue.Queue[tuple[str, object]] = queue.Queue()
        self._scan_thread: threading.Thread | None = None

        self._roots: list[Path] = [Path.home() / "Documents"]
        self._groups = []
        self._group_selected_candidates: dict[int, dict[str, bool]] = {}
        self._keep_iid_marker = "__KEEP__"
        self._band_values = (1, 2, 4, 8, 16, 32, 64)

        self._build_ui()
        self._refresh_roots()
        self.after(100, self._drain_queue)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        outer.columnconfigure(0, weight=2)
        outer.columnconfigure(1, weight=5)
        outer.rowconfigure(2, weight=1)

        left = ttk.Frame(outer)
        left.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(6, weight=1)

        ttk.Label(left, text="扫描目录").grid(row=0, column=0, sticky="w")
        self.roots_list = tk.Listbox(left, height=6)
        self.roots_list.grid(row=1, column=0, sticky="nsew")

        root_btns = ttk.Frame(left)
        root_btns.grid(row=2, column=0, sticky="ew", pady=(6, 10))
        root_btns.columnconfigure(0, weight=1)
        root_btns.columnconfigure(1, weight=1)
        ttk.Button(root_btns, text="添加目录…", command=self._add_root).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(root_btns, text="移除选中", command=self._remove_selected_root).grid(row=0, column=1, sticky="ew")

        ttk.Separator(left).grid(row=3, column=0, sticky="ew", pady=10)

        ttk.Label(left, text="文件类型").grid(row=4, column=0, sticky="w")
        self.var_docx = tk.BooleanVar(value=True)
        self.var_xlsx = tk.BooleanVar(value=False)
        self.var_pdf = tk.BooleanVar(value=False)
        self.var_txt = tk.BooleanVar(value=False)
        self.var_md = tk.BooleanVar(value=False)
        types = ttk.Frame(left)
        types.grid(row=5, column=0, sticky="ew", pady=(4, 10))
        ttk.Checkbutton(types, text="DOCX（Word）", variable=self.var_docx).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(types, text="XLSX（Excel）", variable=self.var_xlsx).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(types, text="PDF", variable=self.var_pdf).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(types, text="TXT", variable=self.var_txt).grid(row=3, column=0, sticky="w")
        ttk.Checkbutton(types, text="MD", variable=self.var_md).grid(row=4, column=0, sticky="w")

        params = ttk.LabelFrame(left, text="参数（一般不用改）", padding=8)
        params.grid(row=6, column=0, sticky="nsew")
        params.columnconfigure(2, weight=1)

        self.var_within_days = tk.IntVar(value=30)
        self.var_min_similarity = tk.DoubleVar(value=0.82)
        self.var_shingle = tk.IntVar(value=3)
        self.var_max_hamming = tk.IntVar(value=10)
        self.var_bands_idx = tk.IntVar(value=self._band_values.index(8))
        self.var_min_tokens = tk.IntVar(value=80)

        self.var_action = tk.StringVar(value="print")

        help_age = "只处理最近 N 天修改过的文件。"
        help_sim = "越高越严格，越低越宽松。"
        help_shingle = "越小越宽松，越大越严格。"
        help_hamming = "越大越宽松，但可能更慢。"
        help_bands = "用于加速，通常保持默认值。"
        help_tokens = "少于该词数的文件会跳过。"
        help_action = "print 预览；trash 入废纸篓；delete 永久删除。"

        row = 0
        row = self._add_slider(
            params,
            row=row,
            label="近(天内)",
            help_title="近(天内)",
            help_text=help_age,
            variable=self.var_within_days,
            from_v=0,
            to_v=2000,
            resolution=1,
            value_formatter=lambda v: str(int(round(v))),
            left_hint="更近",
            right_hint="更久",
            snap_int=True,
        )
        row = self._add_slider(
            params,
            row=row,
            label="相似度阈值",
            help_title="相似度阈值",
            help_text=help_sim,
            variable=self.var_min_similarity,
            from_v=0.5,
            to_v=1.0,
            resolution=0.01,
            value_formatter=lambda v: f"{v:.2f}",
            left_hint="更宽松",
            right_hint="更严格",
        )
        row = self._add_slider(
            params,
            row=row,
            label="shingle-size",
            help_title="shingle-size",
            help_text=help_shingle,
            variable=self.var_shingle,
            from_v=1,
            to_v=10,
            resolution=1,
            value_formatter=lambda v: str(int(round(v))),
            left_hint="更宽松",
            right_hint="更严格",
            snap_int=True,
        )
        row = self._add_slider(
            params,
            row=row,
            label="max-hamming",
            help_title="max-hamming",
            help_text=help_hamming,
            variable=self.var_max_hamming,
            from_v=0,
            to_v=64,
            resolution=1,
            value_formatter=lambda v: str(int(round(v))),
            left_hint="更严格",
            right_hint="更宽松",
            snap_int=True,
        )
        row = self._add_slider(
            params,
            row=row,
            label="bands",
            help_title="bands",
            help_text=help_bands,
            variable=self.var_bands_idx,
            from_v=0,
            to_v=len(self._band_values) - 1,
            resolution=1,
            value_formatter=lambda v: str(self._band_value_from_index(v)),
            left_hint="更严格",
            right_hint="更宽松",
            snap_int=True,
        )
        row = self._add_slider(
            params,
            row=row,
            label="min-tokens",
            help_title="min-tokens",
            help_text=help_tokens,
            variable=self.var_min_tokens,
            from_v=0,
            to_v=5000,
            resolution=10,
            value_formatter=lambda v: str(int(round(v))),
            left_hint="跳过更少",
            right_hint="跳过更多",
            snap_int=True,
        )
        ttk.Label(params, text="动作").grid(row=row, column=0, sticky="w")
        ttk.Button(params, text="?", width=2, command=lambda: self._show_help("动作", help_action)).grid(
            row=row, column=1, sticky="w"
        )
        ttk.Combobox(params, textvariable=self.var_action, values=["print", "trash", "delete"], state="readonly").grid(
            row=row, column=2, sticky="ew"
        )

        right_top = ttk.Frame(outer)
        right_top.grid(row=0, column=1, sticky="ew")
        right_top.columnconfigure(0, weight=1)
        right_top.columnconfigure(1, weight=1)
        right_top.columnconfigure(2, weight=1)

        self.btn_scan = ttk.Button(right_top, text="开始扫描（不删除）", command=self._start_scan)
        self.btn_scan.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.btn_apply = ttk.Button(right_top, text="对勾选项执行动作…", command=self._apply_action, state="disabled")
        self.btn_apply.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        ttk.Button(right_top, text="清空结果", command=self._clear_results).grid(row=0, column=2, sticky="ew")

        self.status = ttk.Label(outer, text="就绪")
        self.status.grid(row=1, column=1, sticky="ew", pady=(8, 8))

        right = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        right.grid(row=2, column=1, sticky="nsew")
        outer.rowconfigure(2, weight=1)

        groups_frame = ttk.Labelframe(right, text="相似文件组（仅显示：有候选可处理）", padding=6)
        candidates_frame = ttk.Labelframe(right, text="文件列表（含保留文件；勾选要处理的版本）", padding=6)
        log_frame = ttk.Labelframe(right, text="日志", padding=6)

        right.add(groups_frame, weight=2)
        right.add(candidates_frame, weight=3)
        right.add(log_frame, weight=1)

        groups_frame.columnconfigure(0, weight=1)
        groups_frame.rowconfigure(0, weight=1)
        self.groups_tree = ttk.Treeview(
            groups_frame,
            columns=("keep", "mtime", "count"),
            show="headings",
            height=6,
        )
        self.groups_tree.heading("keep", text="保留（最新）")
        self.groups_tree.heading("mtime", text="修改时间")
        self.groups_tree.heading("count", text="候选数")
        self.groups_tree.column("keep", width=520, anchor="w")
        self.groups_tree.column("mtime", width=160, anchor="w")
        self.groups_tree.column("count", width=70, anchor="e")
        self.groups_tree.grid(row=0, column=0, sticky="nsew")
        self.groups_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_group_selected())

        candidates_frame.columnconfigure(0, weight=1)
        candidates_frame.rowconfigure(1, weight=1)

        cand_btns = ttk.Frame(candidates_frame)
        cand_btns.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(cand_btns, text="全选", command=lambda: self._set_all_candidates(True)).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(cand_btns, text="全不选", command=lambda: self._set_all_candidates(False)).grid(row=0, column=1)

        self.cand_tree = ttk.Treeview(
            candidates_frame,
            columns=("sel", "path", "mtime", "size", "sim"),
            show="headings",
            height=10,
        )
        self.cand_tree.heading("sel", text="选中")
        self.cand_tree.heading("path", text="文件")
        self.cand_tree.heading("mtime", text="修改时间")
        self.cand_tree.heading("size", text="大小")
        self.cand_tree.heading("sim", text="相似度(对保留)")
        self.cand_tree.column("sel", width=60, anchor="center")
        self.cand_tree.column("path", width=560, anchor="w")
        self.cand_tree.column("mtime", width=160, anchor="w")
        self.cand_tree.column("size", width=100, anchor="e")
        self.cand_tree.column("sim", width=110, anchor="e")
        self.cand_tree.grid(row=1, column=0, sticky="nsew")
        self.cand_tree.bind("<Button-1>", self._on_candidate_click)

        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=6, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")

    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_help(self, title: str, message: str) -> None:
        messagebox.showinfo(f"参数说明：{title}", message)

    def _band_value_from_index(self, index_value: float | int) -> int:
        idx = int(round(float(index_value)))
        idx = max(0, min(idx, len(self._band_values) - 1))
        return self._band_values[idx]

    def _add_slider(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        label: str,
        help_title: str,
        help_text: str,
        variable: tk.Variable,
        from_v: float,
        to_v: float,
        resolution: float,
        value_formatter,
        left_hint: str,
        right_hint: str,
        snap_int: bool = False,
    ) -> int:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Button(parent, text="?", width=2, command=lambda: self._show_help(help_title, help_text)).grid(
            row=row, column=1, sticky="w"
        )

        slider_wrap = ttk.Frame(parent)
        slider_wrap.grid(row=row, column=2, sticky="ew")
        slider_wrap.columnconfigure(0, weight=1)
        value_var = tk.StringVar()

        def sync_value() -> None:
            raw = float(variable.get())
            if snap_int:
                snapped = int(round(raw))
                snapped = max(int(from_v), min(snapped, int(to_v)))
                if snapped != int(round(raw)):
                    variable.set(snapped)
                raw = float(snapped)
            value_var.set(value_formatter(raw))

        scale = tk.Scale(
            slider_wrap,
            from_=from_v,
            to=to_v,
            resolution=resolution,
            showvalue=False,
            orient=tk.HORIZONTAL,
            variable=variable,
            command=lambda _v: sync_value(),
            highlightthickness=0,
            bd=0,
        )
        scale.grid(row=0, column=0, sticky="ew")
        ttk.Label(slider_wrap, textvariable=value_var, width=7, anchor="e").grid(row=0, column=1, sticky="e", padx=(6, 0))

        ttk.Label(parent, text=f"左:{left_hint}  右:{right_hint}", wraplength=420).grid(
            row=row + 1, column=2, sticky="w", pady=(0, 4)
        )
        sync_value()
        return row + 2

    def _read_scan_params(self) -> tuple[int, float, int, int, int, int]:
        within_days = max(0, min(int(self.var_within_days.get()), 2000))
        min_similarity = max(0.5, min(float(self.var_min_similarity.get()), 1.0))
        shingle_size = max(1, min(int(self.var_shingle.get()), 10))
        max_hamming = max(0, min(int(self.var_max_hamming.get()), 64))
        min_tokens = max(0, min(int(self.var_min_tokens.get()), 1_000_000))
        bands = self._band_value_from_index(self.var_bands_idx.get())

        # Keep UI values clamped/synced to actual runtime values.
        self.var_within_days.set(within_days)
        self.var_min_similarity.set(round(min_similarity, 2))
        self.var_shingle.set(shingle_size)
        self.var_max_hamming.set(max_hamming)
        self.var_bands_idx.set(self._band_values.index(bands))
        self.var_min_tokens.set(min_tokens)

        return within_days, min_similarity, shingle_size, max_hamming, bands, min_tokens

    def _refresh_roots(self) -> None:
        self.roots_list.delete(0, "end")
        for r in self._roots:
            self.roots_list.insert("end", str(r))

    def _add_root(self) -> None:
        path = filedialog.askdirectory(title="选择要扫描的目录")
        if not path:
            return
        p = Path(path).expanduser().resolve()
        if p not in self._roots:
            self._roots.append(p)
        self._refresh_roots()

    def _remove_selected_root(self) -> None:
        sel = list(self.roots_list.curselection())
        if not sel:
            return
        idx = sel[0]
        try:
            del self._roots[idx]
        except Exception:
            return
        self._refresh_roots()

    def _get_exts(self) -> set[str]:
        exts: set[str] = set()
        if self.var_docx.get():
            exts.add("docx")
        if self.var_xlsx.get():
            exts.add("xlsx")
            exts.add("xlsm")
        if self.var_pdf.get():
            exts.add("pdf")
        if self.var_txt.get():
            exts.add("txt")
        if self.var_md.get():
            exts.add("md")
        if not exts:
            exts = {"docx"}
        return exts

    def _clear_results(self) -> None:
        self._groups = []
        self._group_selected_candidates = {}
        for item in self.groups_tree.get_children():
            self.groups_tree.delete(item)
        for item in self.cand_tree.get_children():
            self.cand_tree.delete(item)
        self.btn_apply.configure(state="disabled")
        self.status.configure(text="就绪")

    def _start_scan(self) -> None:
        if self._scan_thread is not None and self._scan_thread.is_alive():
            messagebox.showinfo("提示", "正在扫描中，请稍等…")
            return

        within_days, min_similarity, shingle_size, max_hamming, bands, min_tokens = self._read_scan_params()

        exts = self._get_exts()
        roots = list(self._roots) if self._roots else [Path.home()]

        self._clear_results()
        self.btn_scan.configure(state="disabled")
        self.status.configure(text="扫描中…（不会删除任何文件）")
        self._log(f"开始扫描：{', '.join(str(r) for r in roots)}")

        if "pdf" in exts:
            pdf_ok = False
            try:
                import pypdf  # type: ignore  # noqa: F401

                pdf_ok = True
            except Exception:
                try:
                    import PyPDF2  # type: ignore  # noqa: F401

                    pdf_ok = True
                except Exception:
                    pass
            if not pdf_ok and shutil.which("pdftotext"):
                pdf_ok = True
            if not pdf_ok:
                self._log("提示：未检测到 pypdf/PyPDF2 或 pdftotext，PDF 可能无法提取正文，会被跳过。")

        def worker() -> None:
            try:
                fingerprints, stats = scan_fingerprints(
                    roots,
                    exts=exts,
                    exclude_dirs=[".git", "node_modules", ".Trash", "__pycache__", ".venv"],
                    exclude_files=[],
                    follow_symlinks=False,
                    max_bytes=50 * 1024 * 1024,
                    read_bytes=2 * 1024 * 1024,
                    docx_max_chars=2_000_000,
                    min_tokens=min_tokens,
                    max_tokens=200_000,
                    shingle_size=shingle_size,
                    sketch_size=128,
                    cache_path=".autodelete_cache.sqlite3",
                    progress_cb=lambda s: self._work_q.put(("progress", s)),
                )
                cutoff_ts = dt.datetime.now(dt.timezone.utc).timestamp() - (within_days * 24 * 3600)
                clusters = suggest_similar_groups(
                    fingerprints,
                    max_hamming=max_hamming,
                    min_similarity=min_similarity,
                    bands=bands,
                    min_sketch_len=20,
                    cross_ext=False,
                    max_token_diff_ratio=0.5,
                )
                groups = build_deletion_groups(fingerprints, clusters, cutoff_ts=cutoff_ts)
                self._work_q.put(
                    ("done", {"groups": groups, "stats": stats, "cutoff_ts": cutoff_ts, "within_days": within_days})
                )
            except Exception as e:
                self._work_q.put(("error", str(e)))

        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._work_q.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                stats: ScanStats = payload  # type: ignore[assignment]
                self.status.configure(
                    text=f"扫描中… scanned={stats.scanned} processed={stats.processed} skipped_size={stats.skipped_size} skipped_tokens={stats.skipped_tokens} errors={stats.errors}"
                )
            elif kind == "error":
                self.btn_scan.configure(state="normal")
                self.status.configure(text="扫描失败")
                self._scan_thread = None
                messagebox.showerror("扫描失败", str(payload))
            elif kind == "done":
                data = payload  # type: ignore[assignment]
                self._groups = data["groups"]
                stats = data["stats"]
                cutoff_ts = data["cutoff_ts"]
                within_days = data["within_days"]
                self._log(f"完成：{asdict(stats)}")
                self._log(f"仅候选：修改时间在近 {within_days} 天内（晚于 {fmt_mtime(cutoff_ts)}）")
                self._render_groups()
                self.btn_scan.configure(state="normal")
                self._scan_thread = None
                if self._groups:
                    self.btn_apply.configure(state="normal")
                    self.status.configure(text=f"扫描完成：找到 {len(self._groups)} 个可处理的相似组")
                else:
                    self.status.configure(text=f"扫描完成：近 {within_days} 天内未找到可处理的相似文件组")
            else:
                self._log(f"[warn] unknown event: {kind}")

        self.after(100, self._drain_queue)

    def _render_groups(self) -> None:
        for item in self.groups_tree.get_children():
            self.groups_tree.delete(item)
        self._group_selected_candidates = {}

        for idx, g in enumerate(self._groups):
            self._group_selected_candidates[idx] = {c.path: True for c in g.candidates}
            self.groups_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    g.keep_path,
                    fmt_mtime(g.keep_mtime),
                    str(len(g.candidates)),
                ),
            )

        if self._groups:
            self.groups_tree.selection_set("0")
            self._on_group_selected()

    def _on_group_selected(self) -> None:
        sel = self.groups_tree.selection()
        if not sel:
            return
        group_idx = int(sel[0])
        self._render_candidates(group_idx)

    def _render_candidates(self, group_idx: int) -> None:
        for item in self.cand_tree.get_children():
            self.cand_tree.delete(item)
        g = self._groups[group_idx]
        selected = self._group_selected_candidates.get(group_idx, {})

        self.cand_tree.insert(
            "",
            "end",
            iid=f"{group_idx}|{self._keep_iid_marker}",
            values=(
                "保留",
                g.keep_path,
                fmt_mtime(g.keep_mtime),
                str(g.keep_size),
                "1.000",
            ),
        )
        for c in g.candidates:
            is_sel = selected.get(c.path, True)
            self.cand_tree.insert(
                "",
                "end",
                iid=f"{group_idx}|{c.path}",
                values=(
                    "☑" if is_sel else "☐",
                    c.path,
                    fmt_mtime(c.mtime),
                    str(c.size),
                    f"{c.similarity_to_keep:.3f}",
                ),
            )

    def _on_candidate_click(self, event) -> None:
        region = self.cand_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.cand_tree.identify_column(event.x)
        if col != "#1":
            return
        row_id = self.cand_tree.identify_row(event.y)
        if not row_id:
            return
        try:
            group_idx_str, path = row_id.split("|", 1)
            group_idx = int(group_idx_str)
        except Exception:
            return
        if path == self._keep_iid_marker:
            return
        current = self._group_selected_candidates.get(group_idx, {}).get(path, True)
        self._group_selected_candidates.setdefault(group_idx, {})[path] = not current
        self._render_candidates(group_idx)

    def _set_all_candidates(self, value: bool) -> None:
        sel = self.groups_tree.selection()
        if not sel:
            return
        group_idx = int(sel[0])
        g = self._groups[group_idx]
        self._group_selected_candidates[group_idx] = {c.path: value for c in g.candidates}
        self._render_candidates(group_idx)

    def _collect_selected_candidates(self) -> list[tuple[int, str]]:
        selected: list[tuple[int, str]] = []
        for group_idx, g in enumerate(self._groups):
            sel_map = self._group_selected_candidates.get(group_idx, {})
            for c in g.candidates:
                if sel_map.get(c.path, True):
                    selected.append((group_idx, c.path))
        return selected

    def _apply_action(self) -> None:
        action = self.var_action.get()
        selected = self._collect_selected_candidates()
        if not selected:
            messagebox.showinfo("提示", "没有勾选任何候选文件。")
            return

        if action == "print":
            messagebox.showinfo("提示", "动作是 print（干跑），不会做任何修改。你可以改成 trash/delete 再执行。")
            return

        if action == "delete":
            confirm = simpledialog.askstring("危险操作", "永久删除不可恢复。\n请输入 DELETE 确认：")
            if confirm != "DELETE":
                return
        else:
            if not messagebox.askyesno("确认", f"将对 {len(selected)} 个文件执行：{action}\n继续吗？"):
                return

        # Do it
        processed = 0
        failed = 0
        skipped = 0
        for group_idx, path_str in selected:
            # safety: file must still match original stat
            candidate = next((c for c in self._groups[group_idx].candidates if c.path == path_str), None)
            if candidate is None:
                skipped += 1
                continue
            p = Path(candidate.path)
            try:
                st = p.stat()
            except OSError as e:
                self._log(f"[skip] not found: {candidate.path} ({e})")
                skipped += 1
                continue
            if abs(st.st_mtime - candidate.mtime) > 1e-6 or st.st_size != candidate.size:
                self._log(f"[skip] changed since scan: {candidate.path}")
                skipped += 1
                continue
            try:
                delete_path(p, action=action)
                processed += 1
            except Exception as e:
                failed += 1
                self._log(f"[error] {candidate.path}: {e}")

        self._log(f"完成：processed={processed} failed={failed} skipped={skipped}")
        messagebox.showinfo("完成", f"完成：processed={processed} failed={failed} skipped={skipped}")
        self._start_scan()


def main() -> int:
    try:
        App().mainloop()
    except tk.TclError as e:
        print(f"GUI 启动失败：{e}")
        print("如果你在无界面环境运行，请使用命令行版本：python3 find_similar_old_files.py ...")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
