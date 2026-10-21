"""Local web interface for the safe Fragmento kitchen checker.

The server deliberately binds only to 127.0.0.1, so contact details and reports
remain on this computer. It never submits a payment or an order.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, set_key

from app import parse_checker_output


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
REPORTS = ROOT / "reports"
WORKFLOW = ROOT / "workflow.json"
WORKFLOW_EXAMPLE = ROOT / "workflow.example.json"
ENV_FILE = ROOT / ".env"
CONTACT_FIELDS = [
    "FRAGMENTO_FIRST_NAME", "FRAGMENTO_LAST_NAME", "FRAGMENTO_CONTACT_EMAIL",
    "FRAGMENTO_PHONE", "FRAGMENTO_ADDRESS1", "FRAGMENTO_ADDRESS2",
    "FRAGMENTO_COUNTRY", "FRAGMENTO_CITY", "FRAGMENTO_POSTAL_CODE",
    "FRAGMENTO_DELIVERY_WEEK", "FRAGMENTO_NOTES",
]


class CheckJob:
    """One interactive checker process, safely shared by local browser requests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.phase = "idle"
        self.status = "Gati për kontroll"
        self.overall = "—"
        self.checks: list[list[str]] = []
        self.report_url: str | None = None
        self.error: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.process is not None and self.process.poll() is None,
                "phase": self.phase,
                "status": self.status,
                "overall": self.overall,
                "checks": self.checks,
                "report_url": self.report_url,
                "error": self.error,
            }

    def start(self, kitchen: str) -> tuple[bool, str]:
        if not re.fullmatch(r"\d+", kitchen):
            return False, "Shkruaj numrin e kontratës vetëm me shifra."
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return False, "Kontrolli është tashmë në proces."
            if not WORKFLOW.exists():
                shutil.copyfile(WORKFLOW_EXAMPLE, WORKFLOW)
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                [sys.executable, str(ROOT / "check_kitchen.py"), "--kitchen", kitchen, "--interactive"],
                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=flags,
            )
            self.phase = "starting"
            self.status = "Po hapet faqja publike…"
            self.overall = "NË PROCES"
            self.checks = []
            self.report_url = None
            self.error = None
        threading.Thread(target=self._collect, daemon=True).start()
        return True, "Kontrolli u nis."

    def continue_check(self) -> tuple[bool, str]:
        with self.lock:
            if self.phase not in ("selection", "form") or not self.process or not self.process.stdin:
                return False, "Nuk ka hap për të vazhduar tani."
            if self.process.poll() is not None:
                return False, "Kontrolli nuk është më aktiv."
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            except OSError:
                return False, "Nuk mund të vazhdohet kontrolli."
            self.status = "Po verifikohen të dhënat…"
            self.phase = "checking"
        return True, "Verifikimi vazhdon."

    def _collect(self) -> None:
        with self.lock:
            process = self.process
        if not process or not process.stdout:
            return
        lines: list[str] = []
        try:
            for line in process.stdout:
                marker = line.strip()
                with self.lock:
                    if marker == "SELECTION_READY":
                        self.phase = "selection"
                        self.status = "Zgjidh opsionet në browser, pastaj kliko “Vazhdo”."
                    elif marker == "FORM_READY":
                        self.phase = "form"
                        self.status = "Plotëso formularin në browser, pastaj kliko “Verifiko”."
                    else:
                        lines.append(line)
            returncode = process.wait()
            overall, checks, report = parse_checker_output("".join(lines))
            with self.lock:
                self.phase = "idle"
                self.checks = [[name, state, detail] for name, state, detail in checks]
                self.overall = overall if overall != "UNKNOWN" else "GABIM"
                if report and report.is_file() and REPORTS in report.resolve().parents:
                    self.report_url = "/reports/" + report.name
                    self.status = "Raporti u krijua."
                else:
                    self.status = "Kontrolli përfundoi pa raport."
                if overall == "UNKNOWN":
                    self.error = f"Programi doli me kod {returncode}."
        except OSError as exc:
            with self.lock:
                self.phase = "idle"
                self.overall = "GABIM"
                self.status = "Kontrolli nuk u nis ose nuk përfundoi."
                self.error = str(exc)


JOB = CheckJob()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return  # Keep the local launcher quiet.

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try:
            value = json.loads(self.rfile.read(size).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _file(self, file_path: Path, content_type: str) -> None:
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(JOB.snapshot())
        elif path == "/api/contact":
            values = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
            self._json({key: values.get(key, "") or "" for key in CONTACT_FIELDS})
        elif path.startswith("/reports/"):
            name = Path(path.removeprefix("/reports/")).name
            self._file(REPORTS / name, "text/html; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        data = self._body()
        if path == "/api/start":
            ok, message = JOB.start(str(data.get("kitchen", "")).strip())
            self._json({"ok": ok, "message": message}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
        elif path == "/api/continue":
            ok, message = JOB.continue_check()
            self._json({"ok": ok, "message": message}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
        elif path == "/api/contact":
            for key in CONTACT_FIELDS:
                value = data.get(key, "")
                if not isinstance(value, str):
                    self._json({"ok": False, "message": "Të dhënat nuk janë të vlefshme."}, HTTPStatus.BAD_REQUEST)
                    return
                set_key(ENV_FILE, key, value.strip())
            self._json({"ok": True, "message": "Të dhënat u ruajtën vetëm lokalisht."})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Fragmento web app: http://127.0.0.1:8765", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
