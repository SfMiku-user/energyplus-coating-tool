from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from model_builder import WorkflowError, run_project_workflow


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
SAMPLE_EXCEL = APP_DIR / "sample_projects" / "示例办公楼.xlsx"
DEFAULT_RESULTS = APP_DIR / "test_runs"


def _discover_executable(
    environment_variable: str,
    command_names: tuple[str, ...],
    common_locations: tuple[str, ...],
) -> str:
    """Return a usable local executable without assuming a particular drive."""
    configured = os.environ.get(environment_variable, "").strip().strip('"')
    if configured and Path(configured).is_file():
        return configured
    for command in command_names:
        found = shutil.which(command)
        if found:
            return found
    for location in common_locations:
        candidate = Path(location)
        if candidate.is_file():
            return str(candidate)
    return ""


class CoatingApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("辐射制冷涂料建筑节能计算工具（测试版）")
        self.geometry("1040x760")
        self.minsize(940, 680)
        self.option_add("*Font", ("Microsoft YaHei UI", 10))

        self.excel_var = tk.StringVar()
        self.epw_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(DEFAULT_RESULTS))
        self.openstudio_var = tk.StringVar()
        self.energyplus_var = tk.StringVar()
        self.status_var = tk.StringVar(
            value="选择建筑参数 Excel 和对应城市的 EPW，然后点击开始计算。"
        )
        self.progress_var = tk.IntVar(value=0)
        self.message_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.latest_run_dir: Path | None = None

        self._load_settings()
        self._build_ui()
        self.after(100, self._poll_messages)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="辐射制冷涂料建筑节能计算",
            font=("Microsoft YaHei UI", 20, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "只需提供建筑参数表和气象文件，程序自动制作模型、检查几何、"
                "运行基准/涂层工况并输出制冷节电率。"
            ),
            foreground="#555555",
        ).pack(anchor="w", pady=(4, 18))

        input_frame = ttk.LabelFrame(outer, text="计算输入", padding=14)
        input_frame.pack(fill="x")
        input_frame.columnconfigure(1, weight=1)
        self._path_row(
            input_frame,
            0,
            "建筑参数 Excel",
            self.excel_var,
            self._browse_excel,
        )
        self._path_row(
            input_frame,
            1,
            "建筑所在地 EPW",
            self.epw_var,
            self._browse_epw,
        )
        self._path_row(
            input_frame,
            2,
            "结果保存目录",
            self.output_var,
            self._browse_output,
            directory=True,
        )

        tools_frame = ttk.LabelFrame(
            outer, text="程序位置（首次使用时确认）", padding=14
        )
        tools_frame.pack(fill="x", pady=(12, 0))
        tools_frame.columnconfigure(1, weight=1)
        self._path_row(
            tools_frame,
            0,
            "OpenStudio 3.11",
            self.openstudio_var,
            self._browse_openstudio,
        )
        self._path_row(
            tools_frame,
            1,
            "EnergyPlus 26.1",
            self.energyplus_var,
            self._browse_energyplus,
        )

        action_frame = ttk.Frame(outer)
        action_frame.pack(fill="x", pady=14)
        self.run_button = ttk.Button(
            action_frame,
            text="开始一键计算",
            command=self._start_run,
        )
        self.run_button.pack(side="left")
        ttk.Button(
            action_frame,
            text="打开输入模板",
            command=self._open_template,
        ).pack(side="left", padx=(10, 0))
        self.open_result_button = ttk.Button(
            action_frame,
            text="打开最近结果",
            command=self._open_latest_result,
            state="disabled",
        )
        self.open_result_button.pack(side="left", padx=(10, 0))
        ttk.Label(action_frame, text="完整计算通常需要约 1 分钟").pack(side="right")

        self.progress = ttk.Progressbar(
            outer,
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
        )
        self.progress.pack(fill="x", pady=(0, 12))

        result_frame = ttk.LabelFrame(outer, text="计算结果", padding=10)
        result_frame.pack(fill="both", expand=True)
        columns = ("metric", "baseline", "coating", "change")
        self.tree = ttk.Treeview(
            result_frame,
            columns=columns,
            show="headings",
            height=8,
        )
        headings = {
            "metric": "指标",
            "baseline": "基准工况",
            "coating": "涂层工况",
            "change": "节省/变化率",
        }
        for key, text in headings.items():
            self.tree.heading(key, text=text)
        self.tree.column("metric", width=270, anchor="w")
        self.tree.column("baseline", width=170, anchor="e")
        self.tree.column("coating", width=170, anchor="e")
        self.tree.column("change", width=170, anchor="e")
        self.tree.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            textvariable=self.status_var,
            foreground="#333333",
            wraplength=980,
        ).pack(fill="x", pady=(12, 0))

    def _path_row(
        self,
        parent,
        row: int,
        label: str,
        variable: tk.StringVar,
        command,
        *,
        directory: bool = False,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=6
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", pady=6
        )
        ttk.Button(
            parent,
            text="选择目录…" if directory else "选择…",
            command=command,
        ).grid(row=row, column=2, padx=(10, 0), pady=6)

    def _browse_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="选择建筑参数 Excel",
            filetypes=[("Excel 工作簿", "*.xlsx")],
        )
        if path:
            self.excel_var.set(path)

    def _browse_epw(self) -> None:
        path = filedialog.askopenfilename(
            title="选择建筑所在地 EPW",
            filetypes=[("EnergyPlus Weather", "*.epw")],
        )
        if path:
            self.epw_var.set(path)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title="选择结果保存目录")
        if path:
            self.output_var.set(path)

    def _browse_openstudio(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 openstudio.exe",
            filetypes=[("OpenStudio", "openstudio.exe"), ("可执行文件", "*.exe")],
        )
        if path:
            self.openstudio_var.set(path)

    def _browse_energyplus(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 energyplus.exe",
            filetypes=[("EnergyPlus", "energyplus.exe"), ("可执行文件", "*.exe")],
        )
        if path:
            self.energyplus_var.set(path)

    def _open_template(self) -> None:
        template = APP_DIR / "templates" / "建筑参数输入模板.xlsx"
        if not template.is_file():
            messagebox.showerror("文件不存在", f"找不到输入模板：{template}")
            return
        os.startfile(template)

    def _open_latest_result(self) -> None:
        if self.latest_run_dir and self.latest_run_dir.is_dir():
            os.startfile(self.latest_run_dir)

    def _start_run(self) -> None:
        inputs = {
            "excel": Path(self.excel_var.get().strip().strip('"')),
            "epw": Path(self.epw_var.get().strip().strip('"')),
            "openstudio": Path(self.openstudio_var.get().strip().strip('"')),
            "energyplus": Path(self.energyplus_var.get().strip().strip('"')),
            "output": Path(self.output_var.get().strip().strip('"')),
        }
        for key, label in (
            ("excel", "建筑参数 Excel"),
            ("epw", "EPW 气象文件"),
            ("openstudio", "OpenStudio 程序"),
            ("energyplus", "EnergyPlus 程序"),
        ):
            if not inputs[key].is_file():
                messagebox.showerror("文件不存在", f"请选择有效的{label}。")
                return
        self._save_settings()
        self.run_button.configure(state="disabled")
        self.open_result_button.configure(state="disabled")
        self.progress_var.set(1)
        self.status_var.set("计算已经开始，请勿关闭窗口。")
        for item in self.tree.get_children():
            self.tree.delete(item)
        threading.Thread(
            target=self._run_worker,
            args=(inputs,),
            daemon=True,
        ).start()

    def _run_worker(self, inputs: dict[str, Path]) -> None:
        try:
            result = run_project_workflow(
                inputs["excel"],
                inputs["epw"],
                inputs["openstudio"],
                inputs["energyplus"],
                inputs["output"],
                progress_callback=lambda message, percent: self.message_queue.put(
                    ("progress", (message, percent))
                ),
            )
            self.message_queue.put(("result", result))
        except Exception as exc:
            self.message_queue.put(("error", str(exc)))

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.message_queue.get_nowait()
                if kind == "progress":
                    message, percent = payload
                    self.progress_var.set(int(percent))
                    self.status_var.set(str(message))
                elif kind == "error":
                    self.run_button.configure(state="normal")
                    self.status_var.set("计算失败，请根据提示修改输入后重试。")
                    messagebox.showerror("计算失败", str(payload))
                elif kind == "result":
                    self.run_button.configure(state="normal")
                    self.progress_var.set(100)
                    self.latest_run_dir = payload.run_dir
                    self.open_result_button.configure(state="normal")
                    self._show_result(payload.result)
                    self.status_var.set(f"计算完成。结果保存在：{payload.run_dir}")
        except queue.Empty:
            pass
        self.after(100, self._poll_messages)

    def _show_result(self, result: dict[str, object]) -> None:
        labels = {
            "cooling_electricity_kwh": ("制冷主设备用电", "kWh"),
            "cooling_system_electricity_kwh": ("制冷系统综合用电", "kWh"),
            "facility_electricity_kwh": ("建筑总用电", "kWh"),
            "heating_electricity_kwh": ("供暖用电", "kWh"),
            "peak_cooling_kw": ("制冷峰值功率", "kW"),
        }
        metrics = result["metrics"]
        for key, values in metrics.items():
            label, unit = labels[key]
            percent = values.get("saving_percent", values.get("change_percent"))
            percent_text = "—" if percent is None else f"{percent:.3f}%"
            self.tree.insert(
                "",
                "end",
                values=(
                    label,
                    f"{values['baseline']:.3f} {unit}",
                    f"{values['coating']:.3f} {unit}",
                    percent_text,
                ),
            )

    def _load_settings(self) -> None:
        data: dict[str, object] = {}
        if SETTINGS_PATH.is_file():
            try:
                loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                pass
        self.excel_var.set(
            str(data.get("excel") or (SAMPLE_EXCEL if SAMPLE_EXCEL.is_file() else ""))
        )
        self.epw_var.set(str(data.get("epw") or ""))
        self.output_var.set(str(data.get("output") or DEFAULT_RESULTS))
        self.openstudio_var.set(
            str(
                data.get("openstudio")
                or _discover_executable(
                    "OPENSTUDIO_EXE",
                    ("openstudio.exe", "openstudio"),
                    (
                        "C:/OpenStudio-3.11.0/bin/openstudio.exe",
                        "C:/Program Files/OpenStudio 3.11.0/bin/openstudio.exe",
                    ),
                )
            )
        )
        self.energyplus_var.set(
            str(
                data.get("energyplus")
                or _discover_executable(
                    "ENERGYPLUS_EXE",
                    ("energyplus.exe", "energyplus"),
                    (
                        "C:/EnergyPlusV26-1-0/energyplus.exe",
                        "C:/EnergyPlus-26-1-0/energyplus.exe",
                    ),
                )
            )
        )

    def _save_settings(self) -> None:
        SETTINGS_PATH.write_text(
            json.dumps(
                {
                    "excel": self.excel_var.get(),
                    "epw": self.epw_var.get(),
                    "output": self.output_var.get(),
                    "openstudio": self.openstudio_var.get(),
                    "energyplus": self.energyplus_var.get(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    try:
        CoatingApp().mainloop()
    except WorkflowError as exc:
        messagebox.showerror("错误", str(exc))
