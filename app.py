"""Small Windows desktop front end for the safe Fragmento checker."""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from dotenv import dotenv_values, set_key


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
EXAMPLE_ENV = ROOT / ".env.example"
WORKFLOW = ROOT / "workflow.json"
WORKFLOW_EXAMPLE = ROOT / "workflow.example.json"
REPORTS = ROOT / "reports"

BG = "#111a27"
PANEL = "#1b293a"
PANEL_LIGHT = "#23354a"
TEXT = "#f4f7fb"
MUTED = "#a7b9ca"
ACCENT = "#57d4b0"
FAIL = "#ff8c83"
FONT = "Segoe UI"

CONTACT_FIELDS = [
    ("Emri", "FRAGMENTO_FIRST_NAME"),
    ("Mbiemri", "FRAGMENTO_LAST_NAME"),
    ("Emaili i porosisë", "FRAGMENTO_CONTACT_EMAIL"),
    ("Telefoni", "FRAGMENTO_PHONE"),
    ("Rruga dhe numri", "FRAGMENTO_ADDRESS1"),
    ("Adresa shtesë (opsionale)", "FRAGMENTO_ADDRESS2"),
    ("Shteti (si në faqe)", "FRAGMENTO_COUNTRY"),
    ("Qyteti (si në faqe)", "FRAGMENTO_CITY"),
    ("Kodi postar (si në faqe)", "FRAGMENTO_POSTAL_CODE"),
    ("Java e dorëzimit", "FRAGMENTO_DELIVERY_WEEK"),
    ("Shënime (opsionale)", "FRAGMENTO_NOTES"),
]

RESULT_RE = re.compile(
    r"^- (.*): (PASS|MATCH|FAIL|MISMATCH|NOT_FOUND|UNCONFIGURED|INCOMPLETE|NOT_RUN|EMAIL_NOT_FOUND|AMBIGUOUS|LOGIN_FAILURE)(?:\s+[—-]\s+(.*))?$"
)


def parse_checker_output(output: str) -> tuple[str, list[tuple[str, str, str]], Path | None]:
    overall = "UNKNOWN"
    checks = []
    report = None
    for line in output.splitlines():
        if line.startswith("Kitchen ") and ": " in line:
            overall = line.rsplit(": ", 1)[-1].strip()
        elif line.startswith("Report: "):
            report = Path(line[len("Report: "):].strip())
        else:
            match = RESULT_RE.match(line)
            if match:
                checks.append((match.group(1), match.group(2), match.group(3) or ""))
    return overall, checks, report


class FragmentoApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Fragmento · Kitchen QA")
        self.geometry("1110x730")
        self.minsize(850, 590)
        self.configure(bg=BG)
        self.events: queue.Queue = queue.Queue()
        self.report_path: Path | None = None
        self.process: subprocess.Popen | None = None
        self.running = False
        self.phase = "idle"
        self.kitchen_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Gati për kontroll")
        self._make_style()
        self._build()
        self.after(120, self._poll)

    def _make_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Qa.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, borderwidth=0, rowheight=36, font=(FONT, 10))
        style.configure("Qa.Treeview.Heading", background=PANEL_LIGHT, foreground=TEXT,
                        borderwidth=0, font=(FONT, 10, "bold"))
        style.map("Qa.Treeview", background=[("selected", "#35546a")])
        style.configure("Qa.Horizontal.TProgressbar", troughcolor=PANEL_LIGHT,
                        background=ACCENT, bordercolor=PANEL_LIGHT)

    def _label(self, parent, text: str, size: int = 11, color: str = TEXT, bold: bool = False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=(FONT, size, "bold" if bold else "normal"), **kwargs)

    def _button(self, parent, text: str, command, primary: bool = False) -> tk.Button:
        return tk.Button(parent, text=text, command=command, cursor="hand2",
                         bg=ACCENT if primary else PANEL_LIGHT,
                         fg=BG if primary else TEXT, activebackground="#77e4c4" if primary else "#34506a",
                         activeforeground=BG if primary else TEXT, relief="flat", borderwidth=0,
                         font=(FONT, 11, "bold"), padx=14, pady=10)

    def _build(self) -> None:
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=30, pady=(26, 20))
        self._label(header, "FRAGMENTO  /  QA", 12, ACCENT, True).pack(anchor="w")
        self._label(header, "Kontrollo kuzhinën para porosisë", 23, TEXT, True).pack(anchor="w", pady=(5, 0))
        self._label(header, "Udhëtimi publik · pa Admin · pa pagesë ose dërgim porosie", 11, MUTED).pack(anchor="w", pady=(5, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=30, pady=(0, 24))
        left = tk.Frame(body, bg=PANEL, width=305)
        left.pack(side="left", fill="y", padx=(0, 18))
        left.pack_propagate(False)
        right = tk.Frame(body, bg=PANEL)
        right.pack(side="left", fill="both", expand=True)

        self._label(left, "KONTRATA", 10, MUTED, True).pack(anchor="w", padx=22, pady=(25, 8))
        self.entry = tk.Entry(left, textvariable=self.kitchen_var, bg="#f8fafc", fg=BG,
                              insertbackground=BG, relief="flat", font=(FONT, 17), justify="left")
        self.entry.pack(fill="x", padx=22, ipady=11)
        self.entry.bind("<Return>", lambda _event: self.start_check())
        self._label(left, "Numri i kontratës (p.sh. 111105807)", 10, MUTED).pack(anchor="w", padx=22, pady=(8, 18))
        self.run_button = self._button(left, "Nis kontrollin", self.start_check, primary=True)
        self.run_button.pack(fill="x", padx=22)
        self.continue_button = self._button(left, "E plotësova formën · Verifiko", self.continue_form)
        self.continue_button.configure(state="disabled")
        self.continue_button.configure(text="Zgjodha opsionet · Vazhdo")
        self.continue_button.pack(fill="x", padx=22, pady=(9, 0))
        self.progress = ttk.Progressbar(left, mode="indeterminate", style="Qa.Horizontal.TProgressbar")
        self.progress.pack(fill="x", padx=22, pady=(17, 0))
        tk.Label(left, textvariable=self.status_var, bg=PANEL, fg=MUTED, wraplength=250,
                 justify="left", font=(FONT, 10)).pack(anchor="w", padx=22, pady=(12, 24))

        tk.Frame(left, bg="#34485c", height=1).pack(fill="x", padx=22)
        self._label(left, "VEPRIME", 10, MUTED, True).pack(anchor="w", padx=22, pady=(22, 10))
        self._button(left, "Të dhënat e kontaktit", self.edit_contact).pack(fill="x", padx=22, pady=(0, 9))
        self._button(left, "Hap raportin", self.open_report).pack(fill="x", padx=22, pady=(0, 9))
        self._button(left, "Hap dosjen e raporteve", self.open_reports_folder).pack(fill="x", padx=22, pady=(0, 9))
        self._label(left, "Plotëso të dhënat në browser-in që hapet.\nMos fut kartelë në aplikacion.\nButoni i pagesës është i çaktivizuar.",
                    10, MUTED, justify="left", wraplength=250).pack(anchor="w", padx=22, pady=(20, 0))

        results_head = tk.Frame(right, bg=PANEL)
        results_head.pack(fill="x", padx=22, pady=(23, 13))
        self._label(results_head, "Rezultatet", 17, TEXT, True).pack(side="left")
        self.overall = self._label(results_head, "—", 12, MUTED, True)
        self.overall.pack(side="right")
        table_frame = tk.Frame(right, bg=PANEL)
        table_frame.pack(fill="both", expand=True, padx=22, pady=(0, 16))
        self.table = ttk.Treeview(table_frame, columns=("check", "status", "detail"), show="headings", style="Qa.Treeview")
        self.table.heading("check", text="Kontrolli")
        self.table.heading("status", text="Statusi")
        self.table.heading("detail", text="Detajet")
        self.table.column("check", width=255, stretch=True)
        self.table.column("status", width=110, stretch=False)
        self.table.column("detail", width=400, stretch=True)
        self.table.tag_configure("good", foreground=ACCENT)
        self.table.tag_configure("bad", foreground=FAIL)
        self.table.tag_configure("pending", foreground="#f0c57a")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._label(right, "Për hollësi dhe pamjet para/pas, hape raportin HTML.", 10, MUTED).pack(anchor="w", padx=22, pady=(0, 20))

    def start_check(self) -> None:
        if self.running:
            return
        kitchen = self.kitchen_var.get().strip()
        if not re.fullmatch(r"\d+", kitchen):
            messagebox.showerror("Numër i pavlefshëm", "Shkruaj numrin e kontratës vetëm me shifra.")
            return
        if not WORKFLOW.exists():
            shutil.copyfile(WORKFLOW_EXAMPLE, WORKFLOW)
        self.running = True
        self.phase = "selection"
        self.run_button.configure(state="disabled")
        self.continue_button.configure(state="disabled")
        self.progress.start(12)
        self.status_var.set("Po kontrollohet faqja publike…")
        self.overall.configure(text="NË PROCES", fg=MUTED)
        for item in self.table.get_children():
            self.table.delete(item)
        threading.Thread(target=self._run_worker, args=(kitchen,), daemon=True).start()

    def _run_worker(self, kitchen: str) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        command = [sys.executable, str(ROOT / "check_kitchen.py"), "--kitchen", kitchen, "--interactive"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, env=env,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace",
                                            bufsize=1, creationflags=flags)
            lines = []
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if line.strip() == "SELECTION_READY":
                    self.events.put(("selection_ready",))
                elif line.strip() == "FORM_READY":
                    self.events.put(("form_ready",))
                else:
                    lines.append(line)
            code = self.process.wait()
            self.events.put(("done", code, "".join(lines), ""))
        except OSError as exc:
            self.events.put(("error", str(exc)))

    def continue_form(self) -> None:
        process = self.process
        if not process or not process.stdin or process.poll() is not None:
            return
        try:
            process.stdin.write("\n")
            process.stdin.flush()
            self.continue_button.configure(state="disabled")
            self.status_var.set("Po verifikohen zgjedhjet…" if self.phase == "selection" else "Po krahasohen të dhënat dhe po krijohet raporti…")
        except OSError as exc:
            self._fail(str(exc))

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "done":
                    self._finish(*event[1:])
                elif event[0] == "selection_ready":
                    self.phase = "selection"
                    self.continue_button.configure(text="Zgjodha opsionet · Vazhdo", state="normal")
                    self.status_var.set("Zgjidh opsionet në browser, pastaj kliko “Zgjodha opsionet · Vazhdo”.")
                elif event[0] == "form_ready":
                    self.phase = "form"
                    self.continue_button.configure(text="E plotësova formën · Verifiko", state="normal")
                    self.status_var.set("Plotëso formën në browser, pastaj kliko “E plotësova formën · Verifiko”.")
                else:
                    self._fail(event[1])
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _finish(self, returncode: int, stdout: str, stderr: str) -> None:
        self.running = False
        self.phase = "idle"
        self.run_button.configure(state="normal")
        self.continue_button.configure(state="disabled")
        self.progress.stop()
        overall, checks, report = parse_checker_output(stdout)
        self.report_path = report if report and report.is_file() else None
        for name, status, detail in checks:
            tag = "good" if status in ("PASS", "MATCH") else "bad" if status in ("FAIL", "MISMATCH", "NOT_FOUND") else "pending"
            self.table.insert("", "end", values=(name, status, detail), tags=(tag,))
        if overall == "UNKNOWN":
            self._fail(stderr.strip() or stdout.strip() or f"Programi doli me kod {returncode}.")
            return
        color = ACCENT if overall == "PASS" else FAIL if overall == "FAIL" else "#f0c57a"
        self.overall.configure(text=overall, fg=color)
        self.status_var.set("Raporti u krijua. Hape për pamjet dhe hollësitë." if report else "Kontrolli përfundoi pa raport.")

    def _fail(self, detail: str) -> None:
        self.running = False
        self.run_button.configure(state="normal")
        self.continue_button.configure(state="disabled")
        self.progress.stop()
        self.overall.configure(text="GABIM", fg=FAIL)
        self.status_var.set("Kontrolli nuk u nis ose nuk përfundoi.")
        messagebox.showerror("Gabim", detail[:2000])

    def open_report(self) -> None:
        report = self.report_path
        if not report:
            existing = sorted(REPORTS.glob("*.html")) if REPORTS.exists() else []
            report = existing[-1] if existing else None
        if report and report.is_file():
            os.startfile(report)
        else:
            messagebox.showinfo("Raporti", "Ende nuk ka raport. Nis një kontroll.")

    def open_reports_folder(self) -> None:
        REPORTS.mkdir(exist_ok=True)
        os.startfile(REPORTS)

    def edit_contact(self) -> None:
        existing = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
        window = tk.Toplevel(self)
        window.title("Të dhënat lokale të kontaktit")
        window.geometry("690x680")
        window.minsize(590, 580)
        window.configure(bg=BG)
        window.transient(self)
        self._label(window, "Të dhënat e kontaktit", 19, TEXT, True).pack(anchor="w", padx=26, pady=(22, 4))
        self._label(window, "Ruhen vetëm në .env në këtë kompjuter. Nuk dërgohet porosi.", 10, MUTED).pack(anchor="w", padx=26)
        form = tk.Frame(window, bg=BG)
        form.pack(fill="both", expand=True, padx=26, pady=18)
        entries = {}
        for index, (caption, key) in enumerate(CONTACT_FIELDS):
            row, col = divmod(index, 2)
            cell = tk.Frame(form, bg=BG)
            cell.grid(row=row, column=col, sticky="ew", padx=(0, 13) if col == 0 else (13, 0), pady=(0, 13))
            self._label(cell, caption, 10, MUTED).pack(anchor="w", pady=(0, 5))
            entry = tk.Entry(cell, bg="#f8fafc", fg=BG, relief="flat", font=(FONT, 11))
            entry.pack(fill="x", ipady=7)
            entry.insert(0, existing.get(key) or "")
            entries[key] = entry
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)

        def save() -> None:
            if not ENV_FILE.exists():
                shutil.copyfile(EXAMPLE_ENV, ENV_FILE)
            for key, entry in entries.items():
                set_key(str(ENV_FILE), key, entry.get().strip(), quote_mode="always")
            messagebox.showinfo("U ruajt", "Të dhënat u ruajtën lokalisht në .env.", parent=window)
            window.destroy()

        self._button(window, "Ruaj të dhënat", save, primary=True).pack(fill="x", padx=26, pady=(0, 22))


def main() -> None:
    app = FragmentoApp()
    app.mainloop()


if __name__ == "__main__":
    main()
