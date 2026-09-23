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
  - A "Load…" button next to the connection fields reads host/port/
    database/user/password from a plain text file (key=value per line,
    e.g. host=localhost) -- keep one file per database and load whichever
    one you need to switch connections quickly, instead of retyping
    everything. Loading is logged to the Activity panel like everything
    else.
  - A status/activity panel at the bottom, newest entry first, showing
    connection changes plus every command run and its result. Everything
    shown there is also written to a per-session log file in this script's
    own directory, named with the timestamp the app was started (e.g.
    pg_gui_activity_20260101_120000.log). When the app exits -- normally,
    via a signal, or due to a crash -- a final line records which.

Dependencies:
    pip install PyQt5 psycopg2-binary

Run:
    python3 pg_gui_qt.py
"""
import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime

from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal
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
    QListWidgetItem,
    QSplitter,
    QMessageBox,
    QFileDialog,
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

# The directory this script itself lives in -- where the per-session
# activity log file gets created.
APP_DIR = os.path.dirname(os.path.abspath(__file__))


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
# Activity logger: feeds the bottom Activity panel and a per-session log
# file in the script's own directory, named with the timestamp the app was
# started. Kept deliberately simple and crash-safe -- every write is
# flushed immediately, and a failure to open/write the file never raises
# (the app just keeps running with on-screen-only activity).
# ---------------------------------------------------------------------------
class ActivityLogger:
    def __init__(self, app_dir):
        self.start_time = datetime.now()
        filename = f"pg_gui_activity_{self.start_time:%Y%m%d_%H%M%S}.log"
        self.path = os.path.join(app_dir, filename)
        self.error = None
        try:
            # buffering=1 -> line-buffered, so each entry hits disk right
            # away rather than sitting in an in-process buffer that a
            # crash could lose.
            self.file = open(self.path, "a", encoding="utf-8", buffering=1)
            self.file.write(
                f"=== pg_gui activity log started {self.start_time.isoformat(timespec='seconds')} ===\n"
            )
            self.file.flush()
        except OSError as exc:
            self.file = None
            self.error = str(exc)

    def write(self, message):
        """Writes one timestamped line to the log file (if open) and
        returns that line's text for display elsewhere (e.g. the panel)."""
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        if self.file is not None:
            try:
                self.file.write(line + "\n")
                self.file.flush()
            except OSError:
                pass
        return line

    def shutdown(self):
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
            self.file = None


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
# Connection profile files: plain text, one "key=value" per line, so you
# can keep a separate file per database and load it into the Connection
# fields instead of retyping host/port/database/user/password every time.
#
#   # example: staging.conf
#   host=db.staging.internal
#   port=5432
#   database=myapp
#   user=myapp_ro
#   password=hunter2
#
# Lines starting with '#' or ';' are comments; blank lines are ignored.
# Keys are case-insensitive; a few common aliases are accepted (dbname for
# database, username for user, hostname for host, pass for password).
# Unrecognized keys or unparseable lines are collected as warnings rather
# than treated as fatal, so a typo doesn't block loading the rest of the
# file.
# ---------------------------------------------------------------------------
_CONNECTION_FIELD_ALIASES = {
    "host": "host",
    "hostname": "host",
    "port": "port",
    "database": "database",
    "dbname": "database",
    "db": "database",
    "user": "user",
    "username": "user",
    "password": "password",
    "pass": "password",
}

_CONNECTION_FIELDS = ("host", "port", "database", "user", "password")


def parse_connection_file(path):
    """Reads a connection profile file. Returns (values, warnings) where
    values is a dict with a subset of _CONNECTION_FIELDS as keys (only the
    fields actually present in the file), and warnings is a list of
    human-readable strings about anything skipped or not understood.
    Raises OSError/UnicodeDecodeError if the file itself can't be read --
    that's treated as fatal by the caller, unlike a single bad line.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    values = {}
    warnings = []
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            warnings.append(f"line {lineno}: no '=' found, skipped: {raw_line.strip()!r}")
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        field = _CONNECTION_FIELD_ALIASES.get(key)
        if field is None:
            warnings.append(f"line {lineno}: unrecognized key {key!r}, ignored")
            continue
        values[field] = value

    return values, warnings


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
    ACTIVITY_COLORS = {
        "ok": QColor("#1a7f37"),
        "error": QColor("#b00020"),
        "connect": QColor("#0a58ca"),
        "info": None,  # leave at the widget's default palette color
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Postgres Query Tool (Qt)")
        self.resize(1050, 650)

        self.pg = PgConnection()
        self.history = HistoryStore()
        self.activity_log = ActivityLogger(APP_DIR)
        self._exit_logged = False

        self.worker = Worker(self.pg)
        self.worker.connect_ok.connect(self._on_connect_ok)
        self.worker.connect_err.connect(self._on_connect_err)
        self.worker.query_ok.connect(self._on_query_ok)
        self.worker.query_err.connect(self._on_query_err)

        self._build_layout()
        self._refresh_history_list()
        self._set_status("disconnected", "Not connected")

        if self.activity_log.error:
            self.activity_log_label.setText(
                f"Activity log could not be opened ({self.activity_log.error}) -- "
                "this session's activity will not be saved to disk."
            )
            self._log_activity("Could not open activity log file -- continuing without file logging.", kind="error")
        else:
            self._log_activity(f"Application started. Logging to {self.activity_log.path}", kind="info")

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

        root.addWidget(self._build_activity_group())

    def _build_activity_group(self):
        group = QGroupBox("Activity")
        layout = QVBoxLayout(group)

        header = QHBoxLayout()
        self.activity_log_label = QLabel(f"Logging to: {self.activity_log.path}")
        self.activity_log_label.setStyleSheet("color: #666666;")
        header.addWidget(self.activity_log_label)
        header.addStretch(1)
        clear_view_btn = QPushButton("Clear view")
        clear_view_btn.setToolTip("Clears this panel only -- the log file on disk is not affected.")
        clear_view_btn.clicked.connect(lambda: self.activity_list.clear())
        header.addWidget(clear_view_btn)
        layout.addLayout(header)

        self.activity_list = QListWidget()
        self.activity_list.setMaximumHeight(160)
        layout.addWidget(self.activity_list)

        return group

    # -- Activity logging ---------------------------------------------
    @staticmethod
    def _one_line(text, maxlen=140):
        text = " ".join(str(text).split())
        if len(text) > maxlen:
            text = text[: maxlen - 1].rstrip() + "\u2026"
        return text

    def _log_activity(self, message, kind="info"):
        """Writes to the activity log file and inserts the same line at the
        top of the on-screen Activity panel (newest entry first)."""
        line = self.activity_log.write(message)
        item = QListWidgetItem(line)
        color = self.ACTIVITY_COLORS.get(kind)
        if color is not None:
            item.setForeground(color)
        self.activity_list.insertItem(0, item)
        # Keep the panel itself bounded -- the full history lives in the
        # log file regardless, so trimming the widget is just tidiness.
        MAX_PANEL_ITEMS = 500
        while self.activity_list.count() > MAX_PANEL_ITEMS:
            self.activity_list.takeItem(self.activity_list.count() - 1)

    def _build_connection_group(self):
        group = QGroupBox("Connection")
        outer = QVBoxLayout(group)

        fields_row = QHBoxLayout()

        load_btn = QPushButton("Load\u2026")
        load_btn.setToolTip(
            "Load host/port/database/user/password from a text file "
            "(key=value per line, e.g. host=localhost). Keep one file per "
            "database to switch between connections quickly."
        )
        load_btn.clicked.connect(self._on_load_connection_file_clicked)
        fields_row.addWidget(load_btn)

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

    def _on_load_connection_file_clicked(self):
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Load connection file",
            "",
            "Connection files (*.conf *.cfg *.env *.txt);;All files (*)",
        )
        if not path:
            return  # user cancelled

        try:
            values, warnings = parse_connection_file(path)
        except (OSError, UnicodeDecodeError) as exc:
            self._log_activity(f"Failed to load connection file {path}: {self._one_line(exc)}", kind="error")
            QMessageBox.critical(self, "Could not load file", f"Could not read {path}:\n{exc}")
            return

        # Full overwrite: whatever the file doesn't specify is cleared,
        # rather than left over from whichever profile was loaded before --
        # that's what makes switching between profiles predictable instead
        # of accidentally carrying a stale password from a previous file.
        self.host_edit.setText(values.get("host", ""))
        self.port_edit.setText(values.get("port", ""))
        self.db_edit.setText(values.get("database", ""))
        self.user_edit.setText(values.get("user", ""))
        self.password_edit.setText(values.get("password", ""))

        loaded_fields = ", ".join(f for f in _CONNECTION_FIELDS if f in values)
        self._log_activity(
            f"Loaded connection info from {path} ({loaded_fields or 'no recognized fields'})", kind="info"
        )
        if warnings:
            for w in warnings:
                self._log_activity(f"  {path}: {w}", kind="info")
            QMessageBox.warning(
                self,
                "Loaded with warnings",
                f"Loaded {path}, but some lines were skipped:\n\n" + "\n".join(warnings),
            )

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
            info = self.pg.info
            self.pg.disconnect()
            self._log_activity(
                f"Disconnected from {info.get('user')}@{info.get('host')}:{info.get('port')}/{info.get('dbname')}",
                kind="connect",
            )
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
        self._log_activity(f"Connecting to {user}@{host}:{port}/{dbname} ...", kind="info")
        self.connect_btn.setEnabled(False)
        self.worker.do_connect(host, port, dbname, user, password)

    def _on_connect_ok(self):
        info = self.pg.info
        self._set_status(
            "connected",
            f"Connected to {info['user']}@{info['host']}:{info['port']}/{info['dbname']}",
        )
        self._log_activity(
            f"Connected to {info['user']}@{info['host']}:{info['port']}/{info['dbname']}", kind="connect"
        )
        self.connect_btn.setText("Disconnect")
        self.connect_btn.setEnabled(True)

    def _on_connect_err(self, message):
        self._set_status("disconnected", "Not connected")
        self.connect_btn.setEnabled(True)
        self._log_activity(f"Connection failed: {self._one_line(message)}", kind="error")
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
                self._log_activity(
                    f"Command: {self._one_line(raw)}  ->  ERROR: {self._one_line(exc)}", kind="error"
                )
                return
            if help_rows is not None:
                self._render_results(["Command", "Description"], help_rows)
                self.query_status_label.setText(f"{len(help_rows)} command(s)")
                self.history.add(raw, status="ok", rowcount=len(help_rows))
                self._refresh_history_list()
                self._log_activity(
                    f"Command: {self._one_line(raw)}  ->  showed {len(help_rows)} help entries", kind="ok"
                )
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
        self._log_activity(f"Query: {self._one_line(sql)}  ->  {msg}", kind="ok")
        self.run_btn.setEnabled(True)

    def _on_query_err(self, sql, message):
        self._render_results([], [])
        self.query_status_label.setText("Error")
        self._log_activity(f"Query: {self._one_line(sql)}  ->  ERROR: {self._one_line(message)}", kind="error")
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
            self._log_activity("Cleared query history.", kind="info")


def _install_exit_handling(app, window):
    """Wires up logging for every way this app can end: a normal quit, a
    terminating signal (Ctrl-C / SIGTERM), or an unhandled exception. Each
    path is guarded by window._exit_logged so only the first one to fire
    writes the "exiting" line -- whichever happens first is the real
    reason, and later cleanup shouldn't overwrite it with "normally"."""

    def on_about_to_quit():
        if not window._exit_logged:
            window._exit_logged = True
            window._log_activity("Application exiting normally.", kind="info")
        window.activity_log.shutdown()

    app.aboutToQuit.connect(on_about_to_quit)

    def excepthook(exc_type, exc_value, exc_tb):
        if not window._exit_logged:
            window._exit_logged = True
            window._log_activity(
                f"Application exiting due to an unhandled exception: "
                f"{exc_type.__name__}: {window._one_line(exc_value)}",
                kind="error",
            )
            # Full traceback goes to the log file only -- too long to be
            # useful in the on-screen panel.
            window.activity_log.write(
                "Traceback:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    def on_signal(signum, _frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        if not window._exit_logged:
            window._exit_logged = True
            window._log_activity(f"Application terminated by signal {name}.", kind="error")
        window.activity_log.shutdown()
        app.quit()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # The Qt event loop blocks in C++ and won't let Python's signal
    # handlers run until something wakes the interpreter up -- this timer
    # is the standard no-op wake-up tick that makes Ctrl-C/SIGTERM work
    # promptly instead of only after the next UI event.
    wake_timer = QTimer()
    wake_timer.timeout.connect(lambda: None)
    wake_timer.start(250)
    return wake_timer  # caller must keep a reference so it isn't collected


def main():
    if psycopg2 is None:
        print("psycopg2 is not installed. Run: pip install psycopg2-binary")
    app = QApplication(sys.argv)
    window = PgGuiApp()
    _wake_timer = _install_exit_handling(app, window)  # noqa: F841 -- keep alive
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
