#!/usr/bin/env python3
r"""
pg_gui.py - A simple desktop GUI for PostgreSQL.

Features:
  - Connection panel (host/port/database/user/password) with a live
    connected/disconnected status indicator.
  - A SQL input box and a "Run" button; results render in a table for
    SELECT-style queries, or a row-count message for INSERT/UPDATE/DELETE/DDL.
    A subset of psql's backslash meta-commands is also understood --
    \dt, \d [table], \dn, \dv, \dm, \ds, \di, \du/\dg, \l, \df, \? -- each
    translated into the equivalent catalog query psql itself would run.
  - A history panel that records every command you run (with timestamp and
    success/failure), persisted to ~/.pg_gui_history.json so it survives
    between runs. Double-click any history entry to load it back into the
    input box.

Dependencies:
    pip install psycopg2-binary

Run:
    python3 pg_gui.py
"""
import json
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

try:
    import psycopg2
except ImportError:  # pragma: no cover - reported to the user at startup
    psycopg2 = None

HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".pg_gui_history.json")
MAX_HISTORY_ENTRIES = 200


# ---------------------------------------------------------------------------
# Persistent history store (JSON file on disk)
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
# Postgres connection wrapper
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
#
# These aren't SQL -- psql itself intercepts them client-side and runs an
# equivalent catalog query. We do the same thing here for the subset people
# reach for most often, so the GUI recognizes them the way a real psql
# session would.
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
# GUI
# ---------------------------------------------------------------------------
class PgGuiApp:
    STATUS_COLORS = {
        "disconnected": "#c0392b",
        "connecting": "#e67e22",
        "connected": "#27ae60",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Postgres Query Tool")
        self.root.geometry("980x640")

        self.pg = PgConnection()
        self.history = HistoryStore()
        self.result_queue = queue.Queue()

        self._build_layout()
        self._refresh_history_list()
        self._set_status("disconnected", "Not connected")
        self._poll_queue()

    # -- Layout -------------------------------------------------------
    def _build_layout(self):
        self._build_connection_frame()

        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body)
        right = ttk.Frame(body, width=280)
        body.add(left, weight=4)
        body.add(right, weight=1)

        self._build_query_frame(left)
        self._build_history_frame(right)

    def _build_connection_frame(self):
        frame = ttk.LabelFrame(self.root, text="Connection")
        frame.pack(fill=tk.X, padx=8, pady=8)

        fields_row = ttk.Frame(frame)
        fields_row.pack(fill=tk.X, padx=4, pady=(4, 2))

        labels = ["Host", "Port", "Database", "User", "Password"]
        defaults = ["localhost", "5432", "", "", ""]
        self.conn_vars = {}
        for label, default in zip(labels, defaults):
            ttk.Label(fields_row, text=label).pack(side=tk.LEFT, padx=(6, 2))
            var = tk.StringVar(value=default)
            show = "*" if label == "Password" else ""
            width = 6 if label == "Port" else 12
            entry = ttk.Entry(fields_row, textvariable=var, show=show, width=width)
            entry.pack(side=tk.LEFT, padx=(0, 6))
            self.conn_vars[label.lower()] = var

        self.connect_btn = ttk.Button(fields_row, text="Connect", command=self._on_connect_clicked)
        self.connect_btn.pack(side=tk.LEFT, padx=(12, 0))

        status_row = ttk.Frame(frame)
        status_row.pack(fill=tk.X, padx=4, pady=(0, 6))

        self.status_canvas = tk.Canvas(status_row, width=14, height=14, highlightthickness=0)
        self.status_dot = self.status_canvas.create_oval(2, 2, 12, 12, fill="grey")
        self.status_canvas.pack(side=tk.LEFT, padx=(6, 4))

        self.status_label = ttk.Label(status_row, text="Not connected")
        self.status_label.pack(side=tk.LEFT)

    def _build_query_frame(self, parent):
        input_frame = ttk.LabelFrame(parent, text="Query")
        input_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 6))

        self.sql_text = tk.Text(input_frame, height=8, wrap="word", undo=True)
        self.sql_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.sql_text.bind("<Control-Return>", lambda e: self._on_run_clicked())

        btn_row = ttk.Frame(input_frame)
        btn_row.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.run_btn = ttk.Button(btn_row, text="Run  (Ctrl+Enter)", command=self._on_run_clicked)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Clear", command=lambda: self.sql_text.delete("1.0", tk.END)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.query_status_label = ttk.Label(btn_row, text="")
        self.query_status_label.pack(side=tk.LEFT, padx=(16, 0))

        hint = ttk.Label(
            input_frame,
            text="Tip: psql-style commands work too -- \\dt, \\d table, \\dn, \\du, \\l, \\df, \\?",
            foreground="#666666",
        )
        hint.pack(fill=tk.X, padx=6, pady=(0, 4), anchor="w")

        output_frame = ttk.LabelFrame(parent, text="Results")
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.result_tree = ttk.Treeview(output_frame, show="headings")
        vsb = ttk.Scrollbar(output_frame, orient="vertical", command=self.result_tree.yview)
        hsb = ttk.Scrollbar(output_frame, orient="horizontal", command=self.result_tree.xview)
        self.result_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        output_frame.rowconfigure(0, weight=1)
        output_frame.columnconfigure(0, weight=1)

    def _build_history_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="History")
        frame.pack(fill=tk.BOTH, expand=True)

        self.history_list = tk.Listbox(frame, activestyle="none")
        self.history_list.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.history_list.bind("<Double-Button-1>", self._on_history_double_click)

        ttk.Button(frame, text="Clear history", command=self._on_clear_history).pack(
            fill=tk.X, padx=6, pady=(0, 6)
        )

    # -- Status helpers -------------------------------------------------
    def _set_status(self, state, text):
        color = self.STATUS_COLORS.get(state, "grey")
        self.status_canvas.itemconfig(self.status_dot, fill=color)
        self.status_label.config(text=text)

    # -- Connection handling ---------------------------------------------
    def _on_connect_clicked(self):
        if self.pg.is_connected:
            self.pg.disconnect()
            self._set_status("disconnected", "Not connected")
            self.connect_btn.config(text="Connect")
            return

        values = {k: v.get() for k, v in self.conn_vars.items()}
        if not values["database"] or not values["user"]:
            messagebox.showwarning("Missing info", "Database and User are required.")
            return

        self._set_status("connecting", "Connecting ...")
        self.connect_btn.config(state=tk.DISABLED)

        def worker():
            try:
                self.pg.connect(
                    host=values["host"] or "localhost",
                    port=values["port"] or "5432",
                    dbname=values["database"],
                    user=values["user"],
                    password=values["password"],
                )
                self.result_queue.put(("connect_ok", None))
            except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                self.result_queue.put(("connect_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_connect_ok(self):
        info = self.pg.info
        self._set_status("connected", f"Connected to {info['user']}@{info['host']}:{info['port']}/{info['dbname']}")
        self.connect_btn.config(text="Disconnect", state=tk.NORMAL)

    def _on_connect_err(self, message):
        self._set_status("disconnected", "Not connected")
        self.connect_btn.config(state=tk.NORMAL)
        messagebox.showerror("Connection failed", message)

    # -- Query handling ---------------------------------------------
    def _on_run_clicked(self):
        raw = self.sql_text.get("1.0", tk.END).strip()
        if not raw:
            return
        if not self.pg.is_connected:
            messagebox.showwarning("Not connected", "Connect to a database first.")
            return

        if raw.startswith("\\"):
            try:
                exec_sql, params, help_rows = translate_meta_command(raw)
            except MetaCommandError as exc:
                messagebox.showerror("Unknown command", str(exc))
                self.history.add(raw, status="error", error=str(exc))
                self._refresh_history_list()
                return
            if help_rows is not None:
                self._render_results(["Command", "Description"], help_rows)
                self.query_status_label.config(text=f"{len(help_rows)} command(s)")
                self.history.add(raw, status="ok", rowcount=len(help_rows))
                self._refresh_history_list()
                return
        else:
            exec_sql, params = raw, None

        self.run_btn.config(state=tk.DISABLED)
        self.query_status_label.config(text="Running ...")

        def worker():
            try:
                columns, rows, rowcount, elapsed = self.pg.execute(exec_sql, params)
                self.result_queue.put(("query_ok", (raw, columns, rows, rowcount, elapsed)))
            except Exception as exc:  # noqa: BLE001
                self.result_queue.put(("query_err", (raw, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _on_query_ok(self, sql, columns, rows, rowcount, elapsed):
        self._render_results(columns, rows)
        if columns:
            msg = f"{rowcount} row(s) returned in {elapsed:.3f}s"
        else:
            msg = f"OK - {rowcount} row(s) affected in {elapsed:.3f}s"
        self.query_status_label.config(text=msg)
        self.history.add(sql, status="ok", rowcount=rowcount, elapsed=round(elapsed, 3))
        self._refresh_history_list()
        self.run_btn.config(state=tk.NORMAL)

    def _on_query_err(self, sql, message):
        self._render_results([], [])
        self.query_status_label.config(text="Error")
        messagebox.showerror("Query failed", message)
        self.history.add(sql, status="error", error=message)
        self._refresh_history_list()
        self.run_btn.config(state=tk.NORMAL)

    def _render_results(self, columns, rows):
        self.result_tree.delete(*self.result_tree.get_children())
        self.result_tree["columns"] = columns
        for col in columns:
            self.result_tree.heading(col, text=col)
            self.result_tree.column(col, width=120, stretch=True)
        for row in rows:
            display_row = ["" if v is None else str(v) for v in row]
            self.result_tree.insert("", tk.END, values=display_row)

    # -- History handling ---------------------------------------------
    def _refresh_history_list(self):
        self.history_list.delete(0, tk.END)
        for entry in reversed(self.history.entries):
            icon = "OK " if entry["status"] == "ok" else "ERR"
            one_line = " ".join(entry["sql"].split())
            label = f"[{icon}] {entry['timestamp']}  {one_line[:60]}"
            self.history_list.insert(tk.END, label)

    def _on_history_double_click(self, _event):
        selection = self.history_list.curselection()
        if not selection:
            return
        # history_list is displayed newest-first; map back to entries list
        index_from_top = selection[0]
        entry = list(reversed(self.history.entries))[index_from_top]
        self.sql_text.delete("1.0", tk.END)
        self.sql_text.insert("1.0", entry["sql"])

    def _on_clear_history(self):
        if messagebox.askyesno("Clear history", "Remove all saved command history?"):
            self.history.clear()
            self._refresh_history_list()

    # -- Background-thread -> UI-thread bridge ---------------------------
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "connect_ok":
                    self._on_connect_ok()
                elif kind == "connect_err":
                    self._on_connect_err(payload)
                elif kind == "query_ok":
                    self._on_query_ok(*payload)
                elif kind == "query_err":
                    self._on_query_err(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main():
    if psycopg2 is None:
        print("psycopg2 is not installed. Run: pip install psycopg2-binary")
    root = tk.Tk()
    PgGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
