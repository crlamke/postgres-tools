#!/usr/bin/env python3
r"""
pg_gui_qt.py - A simple desktop GUI for PostgreSQL, built with PyQt5.

This is a Qt port of pg_gui.py (the Tkinter version) -- same feature set,
different toolkit:

  - Connection panel (host/port/database/user/password) with a live
    connected/disconnected status indicator.
  - A SQL input box and a "Run" button (or Ctrl+Enter); results render in a
    table for SELECT-style queries, or a row-count message for
    INSERT/UPDATE/DELETE/DDL. A subset of psql's backslash meta-commands is
    also understood -- \dt, \d [table], \dn, \dv, \dm, \ds, \di, \du/\dg,
    \l, \df, \? -- each translated into the equivalent catalog query psql
    itself would run.
  - A history panel that records every command you run (with timestamp and
    success/failure), persisted to ~/.pg_gui_history.json so it survives
    between runs. Double-click any entry to load it back into the input box.

Dependencies:
    pip install PyQt5 psycopg2-binary

Run:
    python3 pg_gui_qt.py
"""
import json
import os
import sys
import threading
import time
from datetime import datetime

from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QListWidget,
    QSplitter,
    QMessageBox,
    QHBoxLayout,
    QVBoxLayout,
    QFormLayout,
    QShortcut,
)
from PyQt5.QtGui import QKeySequence

try:
    import psycopg2
except ImportError:  # pragma: no cover - reported to the user at startup
    psycopg2 = None

HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".pg_gui_history.json")
MAX_HISTORY_ENTRIES = 200


# ---------------------------------------------------------------------------
# Persistent history store (JSON file on disk) -- identical to the Tk version
# ---------------------------------------------------------------------------
class HistoryStore:
    """Loads/saves a list of executed-command entries to a JSON file."""

    def __init__(self, path=HISTORY_FILE, max_entries=MAX_HISTORY_ENTRIES):
        self.path = path
        self.max_entries = max_entries
        self.entries = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def add(self, sql, status, rowcount=None, error=None, elapsed=None):
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "sql": sql,
            "status": status,  # "ok" or "error"
            "rowcount": rowcount,
            "error": error,
            "elapsed": elapsed,
        }
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]
        self._save()
        return entry

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, indent=2)
        except OSError:
            pass  # history is a convenience, not critical -- don't crash the app

    def clear(self):
        self.entries = []
        self._save()


# ---------------------------------------------------------------------------
# Postgres connection wrapper -- identical to the Tk version
# ---------------------------------------------------------------------------
class ConnectionError_(Exception):
    pass


class PgConnection:
    """Thin wrapper around a psycopg2 connection, run in autocommit mode so
    every statement takes effect immediately -- the simplest behavior for
    an ad-hoc query tool."""

    def __init__(self):
        self.conn = None
        self.info = {}

    @property
    def is_connected(self):
        return self.conn is not None and not self.conn.closed

    def connect(self, host, port, dbname, user, password, connect_timeout=8):
        if psycopg2 is None:
            raise ConnectionError_(
                "psycopg2 is not installed. Run: pip install psycopg2-binary"
            )
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            connect_timeout=connect_timeout,
        )
        conn.autocommit = True
        self.conn = conn
        self.info = {"host": host, "port": port, "dbname": dbname, "user": user}

    def disconnect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self.info = {}

    def execute(self, sql, params=None):
        """Runs one SQL statement. Returns (columns, rows, rowcount, elapsed)."""
        if not self.is_connected:
            raise ConnectionError_("Not connected to a database.")
        start = time.monotonic()
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is not None:
                columns = [d.name for d in cur.description]
                rows = cur.fetchall()
                rowcount = len(rows)
            else:
                columns, rows = [], []
                rowcount = cur.rowcount
        elapsed = time.monotonic() - start
        return columns, rows, rowcount, elapsed


# ---------------------------------------------------------------------------
# psql-style backslash meta-commands (\dt, \d, \dn, \du, \l, \df, ...)
# -- identical to the Tk version
# ---------------------------------------------------------------------------
class MetaCommandError(ValueError):
    pass


_META_HELP_ROWS = [
    ("\\dt [pattern]", "List tables"),
    ("\\dv [pattern]", "List views"),
    ("\\dm [pattern]", "List materialized views"),
    ("\\ds [pattern]", "List sequences"),
    ("\\di [pattern]", "List indexes"),
    ("\\d [pattern]", "List tables/views/sequences, or (with a table name) describe its columns"),
    ("\\dn [pattern]", "List schemas"),
    ("\\du / \\dg [pattern]", "List roles"),
    ("\\l / \\list [pattern]", "List databases"),
    ("\\df [pattern]", "List functions"),
    ("\\? / \\h", "Show this help"),
]

_RELKIND_LABELS_SQL = """CASE c.relkind
        WHEN 'r' THEN 'table'
        WHEN 'v' THEN 'view'
        WHEN 'm' THEN 'materialized view'
        WHEN 'i' THEN 'index'
        WHEN 'S' THEN 'sequence'
        WHEN 'p' THEN 'partitioned table'
        WHEN 'I' THEN 'partitioned index'
        ELSE c.relkind::text
    END"""


def _relations_query(kinds, pattern):
    sql = f"""
        SELECT n.nspname AS "Schema", c.relname AS "Name",
               {_RELKIND_LABELS_SQL} AS "Type",
               pg_catalog.pg_get_userbyid(c.relowner) AS "Owner"
        FROM pg_catalog.pg_class c
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = ANY(%s)
          AND n.nspname !~ '^pg_toast'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    """
    params = [list(kinds)]
    if pattern:
        sql += ' AND c.relname ILIKE %s'
        params.append(f"%{pattern}%")
    sql += " ORDER BY 1, 2;"
    return sql, tuple(params)


def _describe_table_query(table):
    sql = """
        SELECT a.attname AS "Column",
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS "Type",
               CASE WHEN a.attnotnull THEN 'not null' ELSE '' END AS "Nullable",
               COALESCE(
                   (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid)
                    FROM pg_catalog.pg_attrdef d
                    WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
                   ''
               ) AS "Default"
        FROM pg_catalog.pg_attribute a
        WHERE a.attrelid = %s::regclass
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum;
    """
    return sql, (table,)


def _schemas_query(pattern):
    sql = """
        SELECT n.nspname AS "Name", pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
        FROM pg_catalog.pg_namespace n
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    """
    params = []
    if pattern:
        sql += ' AND n.nspname ILIKE %s'
        params.append(f"%{pattern}%")
    sql += " ORDER BY 1;"
    return sql, tuple(params)


def _roles_query(pattern):
    sql = """
        SELECT r.rolname AS "Role name",
               r.rolsuper AS "Superuser",
               r.rolcreaterole AS "Create role",
               r.rolcreatedb AS "Create DB",
               r.rolcanlogin AS "Can login"
        FROM pg_catalog.pg_roles r
    """
    params = []
    if pattern:
        sql += ' WHERE r.rolname ILIKE %s'
        params.append(f"%{pattern}%")
    sql += " ORDER BY 1;"
    return sql, tuple(params)


def _databases_query(pattern):
    sql = """
        SELECT d.datname AS "Name",
               pg_catalog.pg_get_userbyid(d.datdba) AS "Owner",
               pg_catalog.pg_encoding_to_char(d.encoding) AS "Encoding"
        FROM pg_catalog.pg_database d
    """
    params = []
    if pattern:
        sql += ' WHERE d.datname ILIKE %s'
        params.append(f"%{pattern}%")
    sql += " ORDER BY 1;"
    return sql, tuple(params)


def _functions_query(pattern):
    sql = """
        SELECT n.nspname AS "Schema", p.proname AS "Name",
               pg_catalog.pg_get_function_result(p.oid) AS "Result type",
               pg_catalog.pg_get_function_arguments(p.oid) AS "Argument types"
        FROM pg_catalog.pg_proc p
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    """
    params = []
    if pattern:
        sql += ' AND p.proname ILIKE %s'
        params.append(f"%{pattern}%")
    sql += " ORDER BY 1, 2;"
    return sql, tuple(params)


_META_COMMAND_HANDLERS = {
    "\\dt": lambda pattern: _relations_query(("r", "p"), pattern),
    "\\dv": lambda pattern: _relations_query(("v",), pattern),
    "\\dm": lambda pattern: _relations_query(("m",), pattern),
    "\\ds": lambda pattern: _relations_query(("S",), pattern),
    "\\di": lambda pattern: _relations_query(("i", "I"), pattern),
    "\\dn": _schemas_query,
    "\\du": _roles_query,
    "\\dg": _roles_query,
    "\\l": _databases_query,
    "\\list": _databases_query,
    "\\df": _functions_query,
}


def translate_meta_command(raw):
    """Translates a psql-style backslash command into (sql, params, help_rows).

    Exactly one of (sql, help_rows) is non-None on success. Raises
    MetaCommandError with a user-facing message if the command isn't
    recognized.
    """
    parts = raw.strip().split(None, 1)
    cmd_raw = parts[0]
    pattern = parts[1].strip() if len(parts) > 1 else ""
    base = cmd_raw.rstrip("+")  # accept the "+" (extra detail) variants as-is

    if base in ("\\?", "\\h", "\\help"):
        return None, None, _META_HELP_ROWS

    if base == "\\d":
        if pattern:
            sql, params = _describe_table_query(pattern)
        else:
            sql, params = _relations_query(("r", "v", "m", "S", "p"), "")
        return sql, params, None

    handler = _META_COMMAND_HANDLERS.get(base)
    if handler is None:
        supported = " ".join(sorted(set(_META_COMMAND_HANDLERS) | {"\\d", "\\?"}))
        raise MetaCommandError(f"Unsupported command: {cmd_raw}\nSupported: {supported}")

    sql, params = handler(pattern)
    return sql, params, None


# ---------------------------------------------------------------------------
# Background worker: runs connect()/execute() on a plain Python thread and
# reports back via Qt signals, which Qt automatically marshals onto the main
# (GUI) thread for slots connected the normal way -- no manual polling queue
# needed, unlike the Tkinter version.
# ---------------------------------------------------------------------------
class Worker(QObject):
    connect_ok = pyqtSignal()
    connect_err = pyqtSignal(str)
    query_ok = pyqtSignal(str, list, list, int, object)   # sql, columns, rows, rowcount, elapsed
    query_err = pyqtSignal(str, str)                       # sql, error message

    def __init__(self, pg):
        super().__init__()
        self.pg = pg

    def do_connect(self, host, port, dbname, user, password):
        def run():
            try:
                self.pg.connect(host=host, port=port, dbname=dbname, user=user, password=password)
                self.connect_ok.emit()
            except Exception as exc:  # noqa: BLE001
                self.connect_err.emit(str(exc))

        threading.Thread(target=run, daemon=True).start()

    def do_query(self, display_sql, exec_sql, params):
        def run():
            try:
                columns, rows, rowcount, elapsed = self.pg.execute(exec_sql, params)
                self.query_ok.emit(display_sql, columns, list(rows), rowcount, elapsed)
            except Exception as exc:  # noqa: BLE001
                self.query_err.emit(display_sql, str(exc))

        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Small helper: a colored dot QLabel for the connection status indicator
# ---------------------------------------------------------------------------
def make_status_dot():
    dot = QLabel()
    dot.setFixedSize(14, 14)
    set_dot_color(dot, "#808080")
    return dot


def set_dot_color(dot, hex_color):
    dot.setStyleSheet(
        f"background-color: {hex_color}; border-radius: 7px;"
    )


STATUS_COLORS = {
    "disconnected": "#c0392b",
    "connecting": "#e67e22",
    "connected": "#27ae60",
}


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class PgGuiApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Postgres Query Tool (Qt)")
        self.resize(1050, 650)

        self.pg = PgConnection()
        self.history = HistoryStore()

        self.worker = Worker(self.pg)
        self.worker.connect_ok.connect(self._on_connect_ok)
        self.worker.connect_err.connect(self._on_connect_err)
        self.worker.query_ok.connect(self._on_query_ok)
        self.worker.query_err.connect(self._on_query_err)

        self._build_layout()
        self._refresh_history_list()
        self._set_status("disconnected", "Not connected")

    # -- Layout -------------------------------------------------------
    def _build_layout(self):
        root = QVBoxLayout(self)
        root.addWidget(self._build_connection_group())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_query_widget())
        splitter.addWidget(self._build_history_group())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

    def _build_connection_group(self):
        group = QGroupBox("Connection")
        outer = QVBoxLayout(group)

        fields_row = QHBoxLayout()
        self.host_edit = QLineEdit("localhost")
        self.port_edit = QLineEdit("5432")
        self.port_edit.setFixedWidth(60)
        self.db_edit = QLineEdit()
        self.user_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)

        for label, widget in [
            ("Host", self.host_edit),
            ("Port", self.port_edit),
            ("Database", self.db_edit),
            ("User", self.user_edit),
            ("Password", self.password_edit),
        ]:
            fields_row.addWidget(QLabel(label))
            fields_row.addWidget(widget)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        fields_row.addWidget(self.connect_btn)
        fields_row.addStretch(1)
        outer.addLayout(fields_row)

        status_row = QHBoxLayout()
        self.status_dot = make_status_dot()
        self.status_label = QLabel("Not connected")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        outer.addLayout(status_row)

        return group

    def _build_query_widget(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        query_group = QGroupBox("Query")
        query_layout = QVBoxLayout(query_group)

        self.sql_edit = QPlainTextEdit()
        self.sql_edit.setFixedHeight(160)
        query_layout.addWidget(self.sql_edit)

        run_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.sql_edit)
        run_shortcut.activated.connect(self._on_run_clicked)
        run_shortcut2 = QShortcut(QKeySequence("Ctrl+Enter"), self.sql_edit)
        run_shortcut2.activated.connect(self._on_run_clicked)

        btn_row = QHBoxLayout()
        self.run_btn = QPushButton("Run  (Ctrl+Enter)")
        self.run_btn.clicked.connect(self._on_run_clicked)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.sql_edit.setPlainText(""))
        self.query_status_label = QLabel("")
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(self.query_status_label)
        btn_row.addStretch(1)
        query_layout.addLayout(btn_row)

        hint = QLabel(
            "Tip: psql-style commands work too -- \\dt, \\d table, \\dn, \\du, \\l, \\df, \\?"
        )
        hint.setStyleSheet("color: #666666;")
        query_layout.addWidget(hint)

        layout.addWidget(query_group)

        results_group = QGroupBox("Results")
        results_layout = QVBoxLayout(results_group)
        self.result_table = QTableWidget()
        self.result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        results_layout.addWidget(self.result_table)
        layout.addWidget(results_group, stretch=1)

        return container

    def _build_history_group(self):
        group = QGroupBox("History")
        layout = QVBoxLayout(group)

        self.history_list = QListWidget()
        self.history_list.itemDoubleClicked.connect(self._on_history_double_click)
        layout.addWidget(self.history_list)

        clear_btn = QPushButton("Clear history")
        clear_btn.clicked.connect(self._on_clear_history)
        layout.addWidget(clear_btn)

        return group

    # -- Status helpers -------------------------------------------------
    def _set_status(self, state, text):
        set_dot_color(self.status_dot, STATUS_COLORS.get(state, "#808080"))
        self.status_label.setText(text)

    # -- Connection handling ---------------------------------------------
    def _on_connect_clicked(self):
        if self.pg.is_connected:
            self.pg.disconnect()
            self._set_status("disconnected", "Not connected")
            self.connect_btn.setText("Connect")
            return

        host = self.host_edit.text() or "localhost"
        port = self.port_edit.text() or "5432"
        dbname = self.db_edit.text()
        user = self.user_edit.text()
        password = self.password_edit.text()

        if not dbname or not user:
            QMessageBox.warning(self, "Missing info", "Database and User are required.")
            return

        self._set_status("connecting", "Connecting ...")
        self.connect_btn.setEnabled(False)
        self.worker.do_connect(host, port, dbname, user, password)

    def _on_connect_ok(self):
        info = self.pg.info
        self._set_status(
            "connected",
            f"Connected to {info['user']}@{info['host']}:{info['port']}/{info['dbname']}",
        )
        self.connect_btn.setText("Disconnect")
        self.connect_btn.setEnabled(True)

    def _on_connect_err(self, message):
        self._set_status("disconnected", "Not connected")
        self.connect_btn.setEnabled(True)
        QMessageBox.critical(self, "Connection failed", message)

    # -- Query handling ---------------------------------------------
    def _on_run_clicked(self):
        raw = self.sql_edit.toPlainText().strip()
        if not raw:
            return
        if not self.pg.is_connected:
            QMessageBox.warning(self, "Not connected", "Connect to a database first.")
            return

        if raw.startswith("\\"):
            try:
                exec_sql, params, help_rows = translate_meta_command(raw)
            except MetaCommandError as exc:
                QMessageBox.critical(self, "Unknown command", str(exc))
                self.history.add(raw, status="error", error=str(exc))
                self._refresh_history_list()
                return
            if help_rows is not None:
                self._render_results(["Command", "Description"], help_rows)
                self.query_status_label.setText(f"{len(help_rows)} command(s)")
                self.history.add(raw, status="ok", rowcount=len(help_rows))
                self._refresh_history_list()
                return
        else:
            exec_sql, params = raw, None

        self.run_btn.setEnabled(False)
        self.query_status_label.setText("Running ...")
        self.worker.do_query(raw, exec_sql, params)

    def _on_query_ok(self, sql, columns, rows, rowcount, elapsed):
        self._render_results(columns, rows)
        if columns:
            msg = f"{rowcount} row(s) returned in {elapsed:.3f}s"
        else:
            msg = f"OK - {rowcount} row(s) affected in {elapsed:.3f}s"
        self.query_status_label.setText(msg)
        self.history.add(sql, status="ok", rowcount=rowcount, elapsed=round(elapsed, 3))
        self._refresh_history_list()
        self.run_btn.setEnabled(True)

    def _on_query_err(self, sql, message):
        self._render_results([], [])
        self.query_status_label.setText("Error")
        QMessageBox.critical(self, "Query failed", message)
        self.history.add(sql, status="error", error=message)
        self._refresh_history_list()
        self.run_btn.setEnabled(True)

    def _render_results(self, columns, rows):
        self.result_table.clear()
        self.result_table.setColumnCount(len(columns))
        self.result_table.setHorizontalHeaderLabels(columns)
        self.result_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                text = "" if value is None else str(value)
                self.result_table.setItem(r, c, QTableWidgetItem(text))
        self.result_table.resizeColumnsToContents()

    # -- History handling ---------------------------------------------
    def _refresh_history_list(self):
        self.history_list.clear()
        for entry in reversed(self.history.entries):
            icon = "OK " if entry["status"] == "ok" else "ERR"
            one_line = " ".join(entry["sql"].split())
            label = f"[{icon}] {entry['timestamp']}  {one_line[:60]}"
            self.history_list.addItem(label)

    def _on_history_double_click(self, item):
        index_from_top = self.history_list.row(item)
        entry = list(reversed(self.history.entries))[index_from_top]
        self.sql_edit.setPlainText(entry["sql"])

    def _on_clear_history(self):
        reply = QMessageBox.question(
            self,
            "Clear history",
            "Remove all saved command history?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.history.clear()
            self._refresh_history_list()


def main():
    if psycopg2 is None:
        print("psycopg2 is not installed. Run: pip install psycopg2-binary")
    app = QApplication(sys.argv)
    window = PgGuiApp()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
