"""
LinkedIn Enricher -- desktop application.
=========================================

A thin Qt shell over li_engine.  There is no scraping logic in this file: the
engine is driven from a single worker thread and reports back through signals.

Two rules keep this safe:

* Playwright's synchronous API is thread-affine, so every browser object is
  created and used on the one worker thread.  The GUI never touches the engine's
  browser, and the worker never touches a widget.
* GUI -> worker communication goes through `Control` (thread-safe events);
  worker -> GUI goes through signals only.

Run the real app:      python3 li_app.py
Try the interface:     python3 li_app.py --demo     (replays saved profiles, no LinkedIn)
"""

from __future__ import annotations

import logging
import random
import sys
import time
import traceback
from collections import deque
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, QSize,
                            QSortFilterProxyModel, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QIcon, QKeySequence,
                           QPalette, QPixmap, QPainter)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QSizePolicy, QSpinBox,
                               QSplitter, QStyledItemDelegate, QTabWidget, QTableView,
                               QTextBrowser, QVBoxLayout, QWidget)

import li_engine as E
import li_fields as LF
from li_fields import FIELDS

APP_TITLE = "LinkedIn Enricher"
ORG_NAME = "GradNext"

# A restrained palette: this is a work tool, so colour is used only for meaning.
COL_AMBER = "#FFF2CC"
COL_AMBER_LINE = "#D9A400"
COL_RED = "#FFE0E0"
COL_RED_LINE = "#C0392B"
COL_GREEN = "#1E8E3E"
COL_MUTED = "#6B7280"
COL_BORDER = "#D6D9DE"

log = logging.getLogger("enrich")


def human_time(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "estimating..."
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" + (f" {secs}s" if minutes < 5 and secs else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


# ===========================================================================
# log bridge -- engine logging into the Run tab
# ===========================================================================
class QtLogBridge(QObject, logging.Handler):
    """Forwards engine log records to the GUI.

    `emit` runs on whichever thread logged the message, so it must never touch a
    widget: it formats the record there (keeping the mutable LogRecord off the GUI
    thread) and sends only a finished string through a queued signal.
    """

    record = Signal(int, str)

    def __init__(self) -> None:
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        self.setLevel(logging.INFO)

    def emit(self, rec: logging.LogRecord) -> None:
        try:
            self.record.emit(rec.levelno, self.format(rec))
        except Exception:
            pass          # a logging handler must never raise


# ===========================================================================
# the worker thread
# ===========================================================================
class EngineWorker(QThread):
    """Runs one enrichment from start to finish on its own thread.

    QThread is subclassed rather than using moveToThread because the body is a
    single long-running loop: there is no event loop on this thread for queued
    slots to arrive on, so the pause/stop channel is `Control`, not signals.
    """

    stateChanged = Signal(str)
    planReady = Signal(object)
    rowStarted = Signal(int, str)
    rowProgress = Signal(int, str)
    rowFinished = Signal(int, object)
    countersChanged = Signal(dict)
    etaChanged = Signal(float)
    loginNeeded = Signal(str)
    loginResult = Signal(bool, str)
    blocked = Signal(str)
    runFinished = Signal(object)
    failed = Signal(str, str)

    def __init__(self, config: E.AppConfig, creds: E.Credentials | None,
                 demo: bool = False, parent=None):
        super().__init__(parent)
        self.config = config
        self.creds = creds
        self.demo = demo
        self.control = E.Control()
        self.runner: E.EnrichRunner | None = None

    # -- the GUI calls these; they are thread-safe --------------------------
    def request_pause(self) -> None:
        self.control.pause()
        self.stateChanged.emit("paused")

    def request_resume(self) -> None:
        self.control.resume()
        self.stateChanged.emit("running")

    def request_stop(self) -> None:
        self.control.stop()
        self.stateChanged.emit("stopping")

    def run(self) -> None:                   # noqa: C901 -- one linear sequence
        runner = None
        try:
            runner = E.EnrichRunner(self.config, self.control, self.creds)
            self.runner = runner
            self.stateChanged.emit("preparing")
            self.planReady.emit(runner.load())

            if self.demo:
                self._install_demo(runner)
            else:
                runner.open_browser()
                if runner.session_state() != "live":
                    self.stateChanged.emit("login")
                    ok = runner.login(lambda ev: self.loginNeeded.emit(ev.message))
                    self.loginResult.emit(ok, "" if ok else "sign-in was not completed")
                    if not ok:
                        runner.reason = "login failed"
                        self.runFinished.emit(runner.finish())
                        self.stateChanged.emit("done")
                        return

            self.stateChanged.emit("running")
            for event in runner.iter_rows():
                if isinstance(event, E.RowResult):
                    self.rowFinished.emit(event.index, event)
                    self.countersChanged.emit(dict(runner.counters))
                    self.etaChanged.emit(runner.eta_seconds())
                    if event.status == "blocked":
                        self.blocked.emit(event.notes or "unknown")
                elif event.stage == "start":
                    self.rowStarted.emit(event.index, event.name)
                else:
                    self.rowProgress.emit(event.index, event.stage)

            self.runFinished.emit(runner.finish())
            self.stateChanged.emit("done")

        except E.Cancelled:
            try:
                self.runFinished.emit(runner.finish())
            except Exception as exc:
                self.failed.emit(type(exc).__name__, str(exc))
            self.stateChanged.emit("done")
        except E.EngineError as exc:
            self.failed.emit(type(exc).__name__, str(exc))
            self.stateChanged.emit("failed")
        except Exception as exc:
            log.error("the run crashed:\n%s", traceback.format_exc())
            self.failed.emit(type(exc).__name__, str(exc))
            self.stateChanged.emit("failed")
        finally:
            if runner is not None:
                runner.close()       # close the browser on its owning thread

    # -- demo mode ---------------------------------------------------------
    def _install_demo(self, runner: E.EnrichRunner) -> None:
        """Replay the saved profiles so the interface can be exercised offline."""
        import json
        records = {}
        for path in sorted((Path(__file__).resolve().parent / "out").glob("*.json")):
            raw = json.loads(path.read_text())
            prof = raw.get("profile") or {}
            for kind in ("education", "experience"):
                sec = prof.get(kind) or {}
                if (sec.get("full") or {}).get("entries"):
                    sec["entries"] = sec["full"]["entries"]
            records[path.stem] = {
                "slug": path.stem, "url": E.profile_url(path.stem), "ok": True,
                "blocked": None, "profile": prof,
                "education": prof.get("education") or {},
                "experience": prof.get("experience") or {},
                "contact": {"found": True, "email": f"{path.stem[:12]}@example.com",
                            "phone": "", "websites": [], "twitter": ""}
                if hash(path.stem) % 4 == 0 else {"found": False},
            }
        pool = list(records.values())
        control = self.control

        def fake_scrape(page, slug, needs, ctl, cache, gap=(0, 0)):
            ctl.sleep(random.uniform(0.35, 0.9))       # feels like real work
            record = dict(records.get(slug) or random.choice(pool))
            record["slug"], record["url"] = slug, E.profile_url(slug)
            record["pageviews"] = len(needs)
            record["from_cache"] = False
            return record

        def fake_resolve(page, name, email, ctl):
            ctl.sleep(random.uniform(0.3, 0.7))
            pick = random.choice(pool)
            return {"slug": pick["slug"], "confidence": random.choice(
                ["high", "medium", "low"]), "blocked": None, "note": "",
                "matched_name": name, "score": 0.77}

        class DemoSession:
            page = None
            relogins = 0
            def open(self): pass
            def close(self): pass
            def live(self): return True
            def relogin(self, emit): return False

        E.scrape_profile = fake_scrape
        E.resolve_missing_profile = fake_resolve
        runner._session = DemoSession()
        log.info("demo mode: replaying %s saved profiles, nothing is sent to LinkedIn",
                 len(pool))


# ===========================================================================
# the column-mapping model
# ===========================================================================
class ColumnMapModel(QAbstractTableModel):
    """The user's column definitions: header text, source field, on/off."""

    HEADERS = ["Use", "Column heading in your file", "What to put in it", "Cost"]
    COL_ENABLED, COL_HEADER, COL_FIELD, COL_COST = range(4)

    changedSomething = Signal()

    def __init__(self, columns: list[E.ColumnMap] | None = None, parent=None):
        super().__init__(parent)
        self.columns: list[E.ColumnMap] = list(columns or [])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        if role == Qt.DisplayRole and orientation == Qt.Vertical:
            return str(section + 1)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        column = self.columns[index.row()]
        col = index.column()
        spec = FIELDS.get(column.field) if column.field else None

        if role == Qt.CheckStateRole and col == self.COL_ENABLED:
            return Qt.Checked if column.enabled else Qt.Unchecked
        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == self.COL_HEADER:
                return column.header
            if col == self.COL_FIELD:
                return spec.label if spec else "(leave blank for your own notes)"
            if col == self.COL_COST:
                if spec is None:
                    return "—"
                extra = [v for v in spec.needs if v != LF.VISIT_PROFILE]
                if not extra:
                    return "free"
                return "+" + ", ".join(LF.VISIT_LABELS.get(v, v) for v in extra)
        if role == Qt.ToolTipRole:
            if spec is None:
                return ("This column is left alone. Nothing is written into it, so "
                        "you can use it for your own notes.")
            bits = [spec.help or spec.label]
            if spec.experimental:
                bits.append("⚠ Not yet verified against real profiles — "
                            "please check these values.")
            if spec.overwrite == LF.Overwrite.NEVER:
                bits.append("Read-only: this identifies the row and is never changed.")
            return "\n\n".join(bits)
        if role == Qt.ForegroundRole and spec is None:
            return QBrush(QColor(COL_MUTED))
        if role == Qt.BackgroundRole and spec is not None and spec.experimental:
            return QBrush(QColor(COL_AMBER))
        if role == Qt.UserRole:
            return column
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        column = self.columns[index.row()]
        if role == Qt.CheckStateRole and index.column() == self.COL_ENABLED:
            column.enabled = Qt.CheckState(value) == Qt.Checked
        elif role == Qt.EditRole and index.column() == self.COL_HEADER:
            column.header = str(value).strip()
        elif role == Qt.EditRole and index.column() == self.COL_FIELD:
            column.field = value or None
            # a column the user has not named yet takes the field's own label
            if column.field and (not column.header.strip()
                                 or column.header == self.PLACEHOLDER_HEADER):
                column.header = FIELDS[column.field].label
        else:
            return False
        self.dataChanged.emit(self.index(index.row(), 0),
                              self.index(index.row(), self.columnCount() - 1))
        self.changedSomething.emit()
        return True

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == self.COL_ENABLED:
            return base | Qt.ItemIsUserCheckable
        if index.column() in (self.COL_HEADER, self.COL_FIELD):
            return base | Qt.ItemIsEditable
        return base

    # -- editing -----------------------------------------------------------
    PLACEHOLDER_HEADER = "New column"

    def add_column(self, field: str | None = None) -> int:
        row = len(self.columns)
        header = FIELDS[field].label if field else self.PLACEHOLDER_HEADER
        self.beginInsertRows(QModelIndex(), row, row)
        self.columns.append(E.ColumnMap(header=header, field=field, enabled=True))
        self.endInsertRows()
        self.changedSomething.emit()
        return row

    def remove_rows(self, rows: list[int]) -> None:
        for row in sorted(set(rows), reverse=True):
            if 0 <= row < len(self.columns):
                self.beginRemoveRows(QModelIndex(), row, row)
                self.columns.pop(row)
                self.endRemoveRows()
        self.changedSomething.emit()

    def move_row(self, row: int, delta: int) -> int:
        target = row + delta
        if not (0 <= row < len(self.columns) and 0 <= target < len(self.columns)):
            return row
        self.beginResetModel()
        self.columns[row], self.columns[target] = self.columns[target], self.columns[row]
        self.endResetModel()
        self.changedSomething.emit()
        return target

    def set_columns(self, columns: list[E.ColumnMap]) -> None:
        self.beginResetModel()
        self.columns = list(columns)
        self.endResetModel()
        self.changedSomething.emit()

    def enabled_fields(self) -> list[str]:
        return [c.field for c in self.columns if c.enabled and c.field]


class FieldDelegate(QStyledItemDelegate):
    """A combobox of every available field, grouped by category."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItem("(leave blank for your own notes)", None)
        for group, specs in LF.fields_by_group().items():
            if not specs:
                continue
            combo.insertSeparator(combo.count())
            header = combo.count()
            combo.addItem(f"— {group} —", "__group__")
            item = combo.model().item(header)
            item.setEnabled(False)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            for spec in specs:
                label = spec.label + ("  ⚠" if spec.experimental else "")
                combo.addItem(label, spec.key)
                combo.setItemData(combo.count() - 1, spec.help, Qt.ToolTipRole)
        return combo

    def setEditorData(self, editor: QComboBox, index):
        current = index.model().data(index.siblingAtColumn(ColumnMapModel.COL_FIELD),
                                     Qt.UserRole)
        key = current.field if current else None
        position = editor.findData(key)
        editor.setCurrentIndex(max(0, position))

    def setModelData(self, editor: QComboBox, model, index):
        value = editor.currentData()
        if value == "__group__":
            return
        model.setData(index, value, Qt.EditRole)


# ===========================================================================
# the results table
# ===========================================================================
class ResultsModel(QAbstractTableModel):
    """Live table of scraped rows.

    A model, not a QTableWidget: rows arrive one at a time during a run and a
    QTableWidget would allocate an item per cell (tens of thousands of objects for
    a few thousand candidates) and physically reorder rows when sorted, which would
    invalidate the row index the worker uses to address them.  Here sorting is the
    proxy's business and the worker's index stays stable for the life of the run.
    """

    AMBER = QBrush(QColor(COL_AMBER))
    RED = QBrush(QColor(COL_RED))

    def __init__(self, headers: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.headers: list[str] = list(headers or [])
        self.rows: list[dict] = []
        self.meta: list[dict] = []        # status / needs_review / notes / edited
        self._by_index: dict[int, int] = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]
        if role == Qt.DisplayRole and orientation == Qt.Vertical:
            return str(section + 1)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, meta = self.rows[index.row()], self.meta[index.row()]
        header = self.headers[index.column()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            return row.get(header, "")
        if role == Qt.BackgroundRole:
            if meta.get("status") == "error":
                return self.RED
            if meta.get("needs_review"):
                return self.AMBER
        if role == Qt.ToolTipRole:
            parts = []
            value = row.get(header, "")
            if value and len(str(value)) > 40:
                parts.append(str(value))
            if meta.get("notes"):
                parts.append("Note: " + meta["notes"])
            if header in meta.get("edited", set()):
                parts.append("You edited this value.")
            return "\n\n".join(parts) or None
        if role == Qt.FontRole and header in meta.get("edited", set()):
            font = QFont()
            font.setItalic(True)
            font.setBold(True)
            return font
        if role == Qt.UserRole:
            return meta
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        header = self.headers[index.column()]
        self.rows[index.row()][header] = str(value).strip()
        self.meta[index.row()].setdefault("edited", set()).add(header)
        self.dataChanged.emit(index, index)
        return True

    def flags(self, index):
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

    # -- population --------------------------------------------------------
    def reset_columns(self, headers: list[str]) -> None:
        self.beginResetModel()
        self.headers = list(headers)
        self.rows.clear()
        self.meta.clear()
        self._by_index.clear()
        self.endResetModel()

    def upsert(self, result) -> None:
        meta = {"status": result.status, "needs_review": result.needs_review,
                "notes": result.notes, "edited": set(), "index": result.index}
        position = self._by_index.get(result.index)
        if position is None:
            position = len(self.rows)
            self.beginInsertRows(QModelIndex(), position, position)
            self.rows.append(dict(result.values))
            self.meta.append(meta)
            self._by_index[result.index] = position
            self.endInsertRows()
        else:
            edited = self.meta[position].get("edited", set())
            merged = dict(result.values)
            for header in edited:            # never lose a hand correction
                merged[header] = self.rows[position][header]
            meta["edited"] = edited
            self.rows[position] = merged
            self.meta[position] = meta
            self.dataChanged.emit(self.index(position, 0),
                                  self.index(position, self.columnCount() - 1))

    def edited_count(self) -> int:
        return sum(1 for m in self.meta if m.get("edited"))

    def review_count(self) -> int:
        return sum(1 for m in self.meta if m.get("needs_review"))

    def error_count(self) -> int:
        return sum(1 for m in self.meta if m.get("status") == "error")


class ResultsFilter(QSortFilterProxyModel):
    """Search box plus the quick filters on the Data tab."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "all"
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        meta = model.meta[source_row] if source_row < len(model.meta) else {}
        if self.mode == "review" and not meta.get("needs_review"):
            return False
        if self.mode == "errors" and meta.get("status") != "error":
            return False
        if self.mode == "edited" and not meta.get("edited"):
            return False
        text = self.filterRegularExpression().pattern()
        if not text:
            return True
        row = model.rows[source_row]
        needle = text.lower()
        return any(needle in str(v).lower() for v in row.values())


# ===========================================================================
# small shared widgets
# ===========================================================================
def card(title: str) -> tuple[QGroupBox, QVBoxLayout]:
    box = QGroupBox(title)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(9)
    return box, layout


def note(text: str, colour: str = COL_MUTED) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {colour};")
    return label


def banner(text: str, kind: str = "info") -> QFrame:
    frame = QFrame()
    bg, line = {"warn": (COL_AMBER, COL_AMBER_LINE),
                "error": (COL_RED, COL_RED_LINE),
                "info": ("#EAF1FB", "#3B6FB6")}[kind]
    frame.setStyleSheet(
        f"QFrame {{ background: {bg}; border: 1px solid {line};"
        f" border-radius: 6px; }} QLabel {{ background: transparent; }}")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    label = QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label, 1)
    frame.label = label
    return frame


# ===========================================================================
# Config tab
# ===========================================================================
class ConfigTab(QWidget):
    configChanged = Signal()
    testRequested = Signal()
    signInRequested = Signal()

    def __init__(self, config: E.AppConfig, store: E.SecretStore, parent=None):
        super().__init__(parent)
        self.config = config
        self.store = store

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(16, 16, 16, 16)
        self.body.setSpacing(14)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        self._build_input()
        self._build_account()
        self._build_columns()
        self._build_run()
        self._build_output()
        self.body.addStretch(1)
        self.load_from_config()

    # -- 1. where the candidates come from ---------------------------------
    def _build_input(self) -> None:
        box, layout = card("1.  Where your candidates are")
        self.radio_sheet = QRadioButton("A Google Sheet link")
        self.radio_file = QRadioButton("An Excel or CSV file on this computer")
        self.radio_urls = QRadioButton("A list of LinkedIn profile links I paste in")
        for radio in (self.radio_sheet, self.radio_file, self.radio_urls):
            radio.toggled.connect(self._input_mode_changed)
            layout.addWidget(radio)

        self.edit_sheet = QLineEdit()
        self.edit_sheet.setPlaceholderText(
            "https://docs.google.com/spreadsheets/d/.../edit")
        self.edit_sheet.textChanged.connect(self._validate_sheet)
        self.label_sheet = note("")
        layout.addWidget(self.edit_sheet)
        layout.addWidget(self.label_sheet)

        file_row = QHBoxLayout()
        self.edit_file = QLineEdit()
        self.edit_file.setPlaceholderText("No file chosen")
        self.edit_file.setReadOnly(True)
        browse = QPushButton("Choose file...")
        browse.clicked.connect(self._pick_file)
        file_row.addWidget(self.edit_file, 1)
        file_row.addWidget(browse)
        self.widget_file = QWidget()
        self.widget_file.setLayout(file_row)
        layout.addWidget(self.widget_file)

        self.edit_urls = QPlainTextEdit()
        self.edit_urls.setPlaceholderText(
            "Paste LinkedIn profile links, one per line:\n"
            "https://www.linkedin.com/in/someone\n"
            "https://www.linkedin.com/in/someone-else")
        self.edit_urls.setMaximumHeight(110)
        layout.addWidget(self.edit_urls)

        buttons = QHBoxLayout()
        self.button_test = QPushButton("Check I can read it")
        self.button_test.clicked.connect(self.testRequested.emit)
        self.button_match = QPushButton("Use the column names from this file")
        self.button_match.clicked.connect(self.testRequested.emit)
        self.button_match.setToolTip(
            "Reads the file and sets up the columns to match the headings it already has.")
        buttons.addWidget(self.button_test)
        buttons.addWidget(self.button_match)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.label_input_status = note("")
        layout.addWidget(self.label_input_status)
        self.body.addWidget(box)

    def _input_mode_changed(self) -> None:
        mode = self.input_mode()
        self.edit_sheet.setVisible(mode == "sheet")
        self.label_sheet.setVisible(mode == "sheet")
        self.widget_file.setVisible(mode == "file")
        self.edit_urls.setVisible(mode == "urls")
        self.button_match.setEnabled(mode != "urls")
        self.configChanged.emit()

    def input_mode(self) -> str:
        if self.radio_file.isChecked():
            return "file"
        if self.radio_urls.isChecked():
            return "urls"
        return "sheet"

    def _validate_sheet(self, text: str) -> None:
        if not text.strip():
            self.label_sheet.setText("")
            return
        if E.GSHEET_RE.search(text):
            self.label_sheet.setText("That looks like a Google Sheet link.")
            self.label_sheet.setStyleSheet(f"color: {COL_GREEN};")
        else:
            self.label_sheet.setText(
                "That does not look like a Google Sheets link — it should contain "
                "docs.google.com/spreadsheets/d/...")
            self.label_sheet.setStyleSheet(f"color: {COL_RED_LINE};")

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose your candidate list", str(Path.home()),
            "Spreadsheets (*.xlsx *.xlsm *.csv *.tsv);;All files (*)")
        if path:
            self.edit_file.setText(path)
            self.configChanged.emit()

    # -- 2. the LinkedIn account -------------------------------------------
    def _build_account(self) -> None:
        box, layout = card("2.  Your LinkedIn account")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.edit_user = QLineEdit()
        self.edit_user.setPlaceholderText("the email you sign in to LinkedIn with")
        self.edit_pass = QLineEdit()
        self.edit_pass.setEchoMode(QLineEdit.Password)
        self.edit_pass.setPlaceholderText("your LinkedIn password")
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self.edit_pass.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        pass_row = QHBoxLayout()
        pass_row.addWidget(self.edit_pass, 1)
        pass_row.addWidget(show)
        pass_widget = QWidget()
        pass_widget.setLayout(pass_row)
        form.addRow("Email", self.edit_user)
        form.addRow("Password", pass_widget)
        layout.addLayout(form)

        self.check_remember = QCheckBox("Remember my password on this computer")
        self.check_remember.setEnabled(self.store.available)
        layout.addWidget(self.check_remember)

        if self.store.available:
            where = f"Saved in {self.store.name}."
        else:
            where = (f"Your password cannot be saved on this computer "
                     f"({self.store.reason}), so you will be asked to sign in by hand.")
        layout.addWidget(note(where))
        keeper = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "your system")
        layout.addWidget(banner(
            f"Your password is kept by {keeper} for your account only, and is sent "
            "nowhere except to linkedin.com when you sign in. Anything that can run "
            "as you on this computer could read it, so only do this on a machine you "
            "trust. LinkedIn will usually still ask you to approve each new sign-in "
            "by hand — that step is what keeps the saved password from being "
            "enough on its own.", "info"))

        buttons = QHBoxLayout()
        sign_in = QPushButton("Sign in to LinkedIn now")
        sign_in.setToolTip("Opens a browser window so you can get past any security "
                           "check before starting a long run.")
        sign_in.clicked.connect(self.signInRequested.emit)
        forget = QPushButton("Forget my password")
        forget.clicked.connect(self._forget)
        buttons.addWidget(sign_in)
        buttons.addWidget(forget)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.body.addWidget(box)

    def _forget(self) -> None:
        self.store.delete(self.edit_user.text().strip())
        self.edit_pass.clear()
        QMessageBox.information(self, APP_TITLE, "The saved password has been removed.")

    # -- 3. the columns ----------------------------------------------------
    def _build_columns(self) -> None:
        box, layout = card("3.  What to collect, and what to call each column")
        layout.addWidget(note(
            "Each row below is one column in your file. Rename a heading by "
            "double-clicking it, choose what goes in it, or add columns of your own. "
            "Anything set to “leave blank” is never touched, so you can keep "
            "your own notes there."))

        self.map_model = ColumnMapModel()
        self.map_model.changedSomething.connect(self._mapping_changed)
        self.map_view = QTableView()
        self.map_view.setModel(self.map_model)
        self.map_view.setItemDelegateForColumn(ColumnMapModel.COL_FIELD, FieldDelegate())
        self.map_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.map_view.setEditTriggers(QAbstractItemView.DoubleClicked
                                      | QAbstractItemView.SelectedClicked
                                      | QAbstractItemView.EditKeyPressed)
        self.map_view.verticalHeader().setDefaultSectionSize(26)
        header = self.map_view.horizontalHeader()
        header.setSectionResizeMode(ColumnMapModel.COL_ENABLED, QHeaderView.Fixed)
        self.map_view.setColumnWidth(ColumnMapModel.COL_ENABLED, 44)
        header.setSectionResizeMode(ColumnMapModel.COL_HEADER, QHeaderView.Stretch)
        header.setSectionResizeMode(ColumnMapModel.COL_FIELD, QHeaderView.Stretch)
        header.setSectionResizeMode(ColumnMapModel.COL_COST, QHeaderView.ResizeToContents)
        self.map_view.setMinimumHeight(260)

        side = QVBoxLayout()
        for label, slot, tip in (
            ("Add column", self._add_column, "Add a new column to your file."),
            ("Remove", self._remove_columns, "Remove the selected column."),
            ("Move up", lambda: self._move(-1), "Move the column left in your file."),
            ("Move down", lambda: self._move(1), "Move the column right in your file."),
        ):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            side.addWidget(button)
        side.addSpacing(10)
        preset = QPushButton("Standard set")
        preset.setToolTip("Go back to the usual nine columns plus the status columns.")
        preset.clicked.connect(self._load_preset)
        side.addWidget(preset)
        add_group = QPushButton("Add a whole group...")
        add_group.clicked.connect(self._add_group)
        side.addWidget(add_group)
        side.addStretch(1)

        row = QHBoxLayout()
        row.addWidget(self.map_view, 1)
        row.addLayout(side)
        layout.addLayout(row)

        self.label_cost = QLabel()
        self.label_cost.setWordWrap(True)
        layout.addWidget(self.label_cost)
        self.warn_frame = banner("", "warn")
        self.warn_frame.hide()
        layout.addWidget(self.warn_frame)
        self.body.addWidget(box)

    def _mapping_changed(self) -> None:
        self.config.columns = self.map_model.columns
        self._refresh_cost()
        self.configChanged.emit()

    def _refresh_cost(self) -> None:
        fields = self.map_model.enabled_fields()
        visits = LF.required_visits(fields)
        pages = len(visits)
        rows = getattr(self, "_known_row_count", 0)
        text = (f"<b>{pages} LinkedIn page{'s' if pages != 1 else ''} per candidate</b>"
                f" — {LF.describe_visits(visits) or 'nothing to open'}")
        if rows:
            seconds = rows * ((self.spin_min.value() + self.spin_max.value()) / 2
                              + pages * 4)
            text += f". About {human_time(seconds)} for {rows} candidates."
        self.label_cost.setText(text)

        notes = []
        if pages > 5:
            notes.append(f"Opening {pages} pages for every candidate makes it much more "
                         f"likely that LinkedIn pauses your account. Consider turning "
                         f"off the columns you do not really need.")
        experimental = [FIELDS[f].label for f in fields if FIELDS[f].experimental]
        if experimental:
            notes.append("These have not been checked against real profiles yet, so "
                         "please verify them: " + ", ".join(experimental) + ".")
        if notes:
            self.warn_frame.label.setText("  ".join(notes))
            self.warn_frame.show()
        else:
            self.warn_frame.hide()

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.map_view.selectionModel().selectedIndexes()})

    def _add_column(self) -> None:
        row = self.map_model.add_column(None)
        index = self.map_model.index(row, ColumnMapModel.COL_FIELD)
        self.map_view.scrollTo(index)
        self.map_view.setCurrentIndex(index)
        self.map_view.edit(index)

    def _remove_columns(self) -> None:
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, APP_TITLE,
                                    "Click the column you want to remove first.")
            return
        self.map_model.remove_rows(rows)

    def _move(self, delta: int) -> None:
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        new_row = self.map_model.move_row(rows[0], delta)
        self.map_view.selectRow(new_row)

    def _load_preset(self) -> None:
        self.map_model.set_columns(E.default_columns())

    def _add_group(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Add a group of columns")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Which group would you like to add?"))
        combo = QComboBox()
        for group, specs in LF.fields_by_group().items():
            if specs:
                combo.addItem(f"{group}  ({len(specs)} columns)", group)
        layout.addWidget(combo)
        skip = QCheckBox("Leave out the ones that have not been verified yet")
        skip.setChecked(True)
        layout.addWidget(skip)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        group = combo.currentData()
        existing = {c.field for c in self.map_model.columns}
        for spec in LF.fields_by_group().get(group, []):
            if spec.key in existing:
                continue
            if skip.isChecked() and spec.experimental:
                continue
            self.map_model.add_column(spec.key)

    # -- 4. how carefully to work -----------------------------------------
    def _build_run(self) -> None:
        box, layout = card("4.  How carefully to work")
        form = QFormLayout()
        self.spin_min = QDoubleSpinBox()
        self.spin_min.setRange(0.0, 120.0)
        self.spin_min.setSuffix(" s")
        self.spin_max = QDoubleSpinBox()
        self.spin_max.setRange(0.0, 300.0)
        self.spin_max.setSuffix(" s")
        for spin in (self.spin_min, self.spin_max):
            spin.valueChanged.connect(self._pacing_changed)
        pace = QHBoxLayout()
        pace.addWidget(QLabel("between"))
        pace.addWidget(self.spin_min)
        pace.addWidget(QLabel("and"))
        pace.addWidget(self.spin_max)
        pace.addStretch(1)
        pace_widget = QWidget()
        pace_widget.setLayout(pace)
        form.addRow("Wait between candidates", pace_widget)

        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(0, 100000)
        self.spin_limit.setSpecialValueText("all of them")
        form.addRow("Only do the first", self.spin_limit)

        self.spin_ttl = QSpinBox()
        self.spin_ttl.setRange(0, 365)
        self.spin_ttl.setSuffix(" days")
        self.spin_ttl.setSpecialValueText("always fetch fresh")
        form.addRow("Re-use profiles collected within", self.spin_ttl)
        layout.addLayout(form)

        self.check_show_browser = QCheckBox(
            "Show the browser while it works (slower, but you can see what happens)")
        self.check_search = QCheckBox(
            "When there is no LinkedIn link, try to find the person by name")
        self.check_search.setToolTip(
            "LinkedIn cannot be searched by email or phone, so this is a guess based "
            "on the name. Matches are labelled high, medium or low confidence and "
            "flagged for you to check.")
        self.check_cache = QCheckBox(
            "Re-use profiles already collected (much faster, and kinder to LinkedIn)")
        for check in (self.check_show_browser, self.check_search, self.check_cache):
            check.toggled.connect(lambda _=False: self.configChanged.emit())
            layout.addWidget(check)

        self.pacing_warn = banner("", "warn")
        self.pacing_warn.hide()
        layout.addWidget(self.pacing_warn)
        self.body.addWidget(box)

    def _pacing_changed(self) -> None:
        if self.spin_max.value() < self.spin_min.value():
            self.spin_max.setValue(self.spin_min.value())
        if self.spin_min.value() < 4:
            self.pacing_warn.label.setText(
                f"A gap of {self.spin_min.value():g} seconds is risky. LinkedIn watches "
                f"how fast profiles are opened, and accounts that go too quickly get "
                f"paused. 8 seconds or more is much safer.")
            self.pacing_warn.show()
        else:
            self.pacing_warn.hide()
        self._refresh_cost()
        self.configChanged.emit()

    # -- 5. where to save --------------------------------------------------
    def _build_output(self) -> None:
        box, layout = card("5.  Where to save the results")
        row = QHBoxLayout()
        self.edit_out = QLineEdit()
        self.edit_out.setReadOnly(True)
        pick = QPushButton("Change...")
        pick.clicked.connect(self._pick_folder)
        row.addWidget(self.edit_out, 1)
        row.addWidget(pick)
        layout.addLayout(row)

        form = QFormLayout()
        self.edit_stem = QLineEdit()
        form.addRow("File name", self.edit_stem)
        layout.addLayout(form)

        formats = QHBoxLayout()
        self.check_xlsx = QCheckBox("Excel (.xlsx)")
        self.check_csv = QCheckBox("CSV (.csv)")
        self.check_highlight = QCheckBox("Shade the rows that need checking")
        for check in (self.check_xlsx, self.check_csv, self.check_highlight):
            check.toggled.connect(lambda _=False: self.configChanged.emit())
            formats.addWidget(check)
        formats.addStretch(1)
        layout.addLayout(formats)
        layout.addWidget(note(f"Settings are kept in {E.config_path()}"))
        self.body.addWidget(box)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Where should the results go?", self.edit_out.text() or str(Path.home()))
        if folder:
            self.edit_out.setText(folder)
            self.configChanged.emit()

    # -- config <-> widgets ------------------------------------------------
    def load_from_config(self) -> None:
        cfg = self.config
        {"sheet": self.radio_sheet, "file": self.radio_file,
         "urls": self.radio_urls}.get(cfg.input_mode, self.radio_sheet).setChecked(True)
        self.edit_sheet.setText(cfg.sheet_url)
        self.edit_file.setText(cfg.file_path)
        self.edit_urls.setPlainText("\n".join(cfg.urls))
        self.edit_user.setText(cfg.username)
        self.check_remember.setChecked(cfg.remember_password and self.store.available)
        if cfg.username and cfg.remember_password:
            self.edit_pass.setText(self.store.load(cfg.username))
        self.map_model.set_columns([replace(c) for c in cfg.columns] if False else cfg.columns)
        self.spin_min.setValue(cfg.min_delay)
        self.spin_max.setValue(cfg.max_delay)
        self.spin_limit.setValue(cfg.limit)
        self.spin_ttl.setValue(cfg.cache_ttl_days)
        self.check_show_browser.setChecked(not cfg.headless)
        self.check_search.setChecked(cfg.name_search)
        self.check_cache.setChecked(cfg.use_cache)
        self.edit_out.setText(cfg.output_folder)
        self.edit_stem.setText(cfg.output_stem)
        self.check_xlsx.setChecked(cfg.output_xlsx)
        self.check_csv.setChecked(cfg.output_csv)
        self.check_highlight.setChecked(cfg.highlight_needs_review)
        self._input_mode_changed()
        self._refresh_cost()

    def apply_to_config(self) -> E.AppConfig:
        cfg = self.config
        cfg.input_mode = self.input_mode()
        cfg.sheet_url = self.edit_sheet.text().strip()
        cfg.file_path = self.edit_file.text().strip()
        cfg.urls = [l.strip() for l in self.edit_urls.toPlainText().splitlines()
                    if l.strip()]
        cfg.username = self.edit_user.text().strip()
        cfg.remember_password = self.check_remember.isChecked()
        cfg.columns = self.map_model.columns
        cfg.min_delay = self.spin_min.value()
        cfg.max_delay = self.spin_max.value()
        cfg.limit = self.spin_limit.value()
        cfg.cache_ttl_days = self.spin_ttl.value()
        cfg.headless = not self.check_show_browser.isChecked()
        cfg.name_search = self.check_search.isChecked()
        cfg.use_cache = self.check_cache.isChecked()
        cfg.output_folder = self.edit_out.text().strip() or str(E.default_output_dir())
        cfg.output_stem = self.edit_stem.text().strip() or "candidates_enriched"
        cfg.output_xlsx = self.check_xlsx.isChecked()
        cfg.output_csv = self.check_csv.isChecked()
        cfg.highlight_needs_review = self.check_highlight.isChecked()
        return cfg

    def credentials(self) -> E.Credentials:
        return E.Credentials(username=self.edit_user.text().strip(),
                             password=self.edit_pass.text())

    def set_row_count(self, count: int) -> None:
        self._known_row_count = count
        self._refresh_cost()

    def adopt_headers(self, headers: list[str]) -> None:
        """Rebuild the mapping from the headings a file actually has."""
        columns = []
        for header in headers:
            columns.append(E.ColumnMap(header=header,
                                       field=LF.match_header_to_field(header),
                                       enabled=True))
        known = {c.field for c in columns}
        for key in ("found_by", "confidence", "needs_review", "status", "notes"):
            if key not in known:
                columns.append(E.ColumnMap(header=FIELDS[key].label, field=key,
                                           enabled=True))
        self.map_model.set_columns(columns)
        matched = sum(1 for c in columns if c.field)
        self.label_input_status.setText(
            f"Set up {len(columns)} columns from your file; {matched} were recognised "
            f"automatically. Please check the ones marked “leave blank”.")
        self.label_input_status.setStyleSheet(f"color: {COL_GREEN};")


# ===========================================================================
# Run tab
# ===========================================================================
class Counter(QFrame):
    def __init__(self, label: str, tip: str = "", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(f"QFrame {{ border: 1px solid {COL_BORDER};"
                           f" border-radius: 6px; }}")
        if tip:
            self.setToolTip(tip)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(1)
        self.value = QLabel("0")
        font = self.value.font()
        font.setPointSize(font.pointSize() + 7)
        font.setBold(True)
        self.value.setFont(font)
        self.caption = QLabel(label)
        self.caption.setStyleSheet(f"color: {COL_MUTED};")
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set(self, value) -> None:
        self.value.setText(str(value))


class RunTab(QWidget):
    startRequested = Signal()
    pauseRequested = Signal()
    resumeRequested = Signal()
    stopRequested = Signal()
    openFolderRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        controls = QHBoxLayout()
        self.button_start = QPushButton("Start")
        self.button_start.setMinimumWidth(120)
        self.button_start.clicked.connect(self.startRequested.emit)
        self.button_pause = QPushButton("Pause")
        self.button_pause.setEnabled(False)
        self.button_pause.clicked.connect(self._toggle_pause)
        self.button_stop = QPushButton("Stop")
        self.button_stop.setEnabled(False)
        self.button_stop.clicked.connect(self.stopRequested.emit)
        self.button_folder = QPushButton("Open results folder")
        self.button_folder.setEnabled(False)
        self.button_folder.clicked.connect(self.openFolderRequested.emit)
        for button in (self.button_start, self.button_pause, self.button_stop):
            controls.addWidget(button)
        controls.addStretch(1)
        controls.addWidget(self.button_folder)
        layout.addLayout(controls)

        self.label_state = QLabel("Ready when you are.")
        font = self.label_state.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.label_state.setFont(font)
        layout.addWidget(self.label_state)

        self.label_current = QLabel("")
        self.label_current.setStyleSheet(f"color: {COL_MUTED};")
        layout.addWidget(self.label_current)

        self.progress = QProgressBar()
        self.progress.setFormat("%v of %m  (%p%)")
        self.progress.setTextVisible(True)
        self.progress.setMinimumHeight(22)
        layout.addWidget(self.progress)

        self.label_eta = QLabel("")
        self.label_eta.setStyleSheet(f"color: {COL_MUTED};")
        layout.addWidget(self.label_eta)

        counters = QHBoxLayout()
        self.counters = {
            "done": Counter("done", "Candidates processed so far."),
            "filled": Counter("filled in", "Rows where something new was added."),
            "review": Counter("to check", "Rows the app is not confident about."),
            "not_found": Counter("not found", "No matching LinkedIn profile."),
            "errors": Counter("problems", "Rows that failed. See the log below."),
            "pageviews": Counter("pages opened", "LinkedIn pages opened in total."),
        }
        for counter in self.counters.values():
            counters.addWidget(counter)
        counters.addStretch(1)
        layout.addLayout(counters)

        self.notice = banner("", "info")
        self.notice.hide()
        layout.addWidget(self.notice)

        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("Activity"))
        log_row.addStretch(1)
        self.combo_level = QComboBox()
        self.combo_level.addItem("Everything", logging.INFO)
        self.combo_level.addItem("Warnings and problems", logging.WARNING)
        self.combo_level.addItem("Problems only", logging.ERROR)
        log_row.addWidget(self.combo_level)
        self.check_follow = QCheckBox("Follow")
        self.check_follow.setChecked(True)
        log_row.addWidget(self.check_follow)
        layout.addLayout(log_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet("font-family: Menlo, Consolas, monospace;")
        layout.addWidget(self.log_view, 1)

        # Engine logging can produce hundreds of lines a second; batching them on a
        # timer keeps the interface responsive instead of one repaint per line.
        self._pending: deque[tuple[int, str]] = deque(maxlen=4000)
        self._drain = QTimer(self)
        self._drain.setInterval(120)
        self._drain.timeout.connect(self._flush_log)
        self._drain.start()

    def _toggle_pause(self) -> None:
        if self.button_pause.text() == "Pause":
            self.pauseRequested.emit()
        else:
            self.resumeRequested.emit()

    def append_log(self, level: int, text: str) -> None:
        self._pending.append((level, text))

    def _flush_log(self) -> None:
        if not self._pending:
            return
        threshold = self.combo_level.currentData() or logging.INFO
        lines = [text for level, text in self._pending if level >= threshold]
        self._pending.clear()
        if not lines:
            return
        bar = self.log_view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.log_view.appendPlainText("\n".join(lines))
        if self.check_follow.isChecked() and at_bottom:
            bar.setValue(bar.maximum())

    def set_state(self, state: str, detail: str = "") -> None:
        text = {
            "idle": "Ready when you are.",
            "preparing": "Reading your candidate list...",
            "login": "Waiting for you to sign in to LinkedIn",
            "running": "Working through your candidates",
            "paused": "Paused",
            "stopping": "Finishing the current candidate, then stopping...",
            "done": "Finished",
            "failed": "Stopped because of a problem",
        }.get(state, state)
        self.label_state.setText(text + (f" — {detail}" if detail else ""))
        running = state in ("preparing", "login", "running", "paused", "stopping")
        self.button_start.setEnabled(not running)
        self.button_pause.setEnabled(state in ("running", "paused"))
        self.button_stop.setEnabled(running)
        self.button_pause.setText("Resume" if state == "paused" else "Pause")

    def show_notice(self, text: str, kind: str = "info") -> None:
        new = banner(text, kind)
        self.layout().replaceWidget(self.notice, new)
        self.notice.deleteLater()
        self.notice = new
        self.notice.show()

    def hide_notice(self) -> None:
        self.notice.hide()


# ===========================================================================
# Data tab
# ===========================================================================
class DataTab(QWidget):
    exportRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search everything...")
        self.search.setClearButtonEnabled(True)
        top.addWidget(self.search, 1)
        self.filters = {}
        for key, label, tip in (
            ("all", "All", "Every row."),
            ("review", "To check", "Rows the app is not confident about."),
            ("errors", "Problems", "Rows that failed."),
            ("edited", "My edits", "Rows you have changed by hand."),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self.filters[key] = button
            top.addWidget(button)
        self.filters["all"].setChecked(True)
        layout.addLayout(top)

        self.model = ResultsModel()
        self.proxy = ResultsFilter()
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.view.setEditTriggers(QAbstractItemView.DoubleClicked
                                  | QAbstractItemView.EditKeyPressed)
        self.view.verticalHeader().setDefaultSectionSize(24)
        header = self.view.horizontalHeader()
        header.setSectionsMovable(True)
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._column_menu)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setDefaultSectionSize(150)
        layout.addWidget(self.view, 1)

        self.search.textChanged.connect(self.proxy.setFilterFixedString)

        bottom = QHBoxLayout()
        self.label_summary = QLabel("Nothing collected yet.")
        self.label_summary.setStyleSheet(f"color: {COL_MUTED};")
        bottom.addWidget(self.label_summary, 1)
        copy = QPushButton("Copy selection")
        copy.setToolTip("Copies the selected cells so you can paste them into Excel.")
        copy.clicked.connect(self._copy_selection)
        bottom.addWidget(copy)
        self.button_export = QPushButton("Export...")
        self.button_export.setMinimumWidth(120)
        self.button_export.clicked.connect(self.exportRequested.emit)
        bottom.addWidget(self.button_export)
        layout.addLayout(bottom)

        layout.addWidget(note(
            "You can correct any value by double-clicking it — your edits are what "
            "get exported. Shaded rows are ones the app is not confident about."))
        self.model.dataChanged.connect(lambda *_: self.refresh_summary())
        self.model.rowsInserted.connect(lambda *_: self.refresh_summary())

    def _set_filter(self, key: str) -> None:
        for name, button in self.filters.items():
            button.setChecked(name == key)
        self.proxy.set_mode(key)
        self.refresh_summary()

    def focus_filter(self, key: str) -> None:
        self._set_filter(key)

    def _column_menu(self, point) -> None:
        menu = QMenu(self)
        header = self.view.horizontalHeader()
        for column in range(self.model.columnCount()):
            action = QAction(self.model.headers[column], menu)
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(column))
            action.toggled.connect(
                lambda visible, c=column: header.setSectionHidden(c, not visible))
            menu.addAction(action)
        menu.exec(header.mapToGlobal(point))

    def _copy_selection(self) -> None:
        indexes = self.view.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows = {}
        for index in indexes:
            rows.setdefault(index.row(), {})[index.column()] = index.data() or ""
        lines = []
        for row in sorted(rows):
            cells = rows[row]
            lines.append("\t".join(str(cells[c]) for c in sorted(cells)))
        QApplication.clipboard().setText("\n".join(lines))

    def refresh_summary(self) -> None:
        total = self.model.rowCount()
        shown = self.proxy.rowCount()
        if not total:
            self.label_summary.setText("Nothing collected yet.")
            return
        bits = [f"{total} rows"]
        if shown != total:
            bits.append(f"{shown} shown")
        if self.model.review_count():
            bits.append(f"{self.model.review_count()} to check")
        if self.model.error_count():
            bits.append(f"{self.model.error_count()} problems")
        if self.model.edited_count():
            bits.append(f"{self.model.edited_count()} edited by you")
        self.label_summary.setText("  ·  ".join(bits))

    def prepare(self, headers: list[str]) -> None:
        self.model.reset_columns(headers)
        self.refresh_summary()

    def add_result(self, result) -> None:
        self.model.upsert(result)


# ===========================================================================
# background helper for the short, blocking jobs (reading a sheet, exporting)
# ===========================================================================
class OneShot(QThread):
    """Runs a callable off the GUI thread.  Used for reading a sheet (which can
    block for a minute) and for saving a large workbook."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self.fn())
        except Exception as exc:
            self.failed.emit(str(exc))


# ===========================================================================
# main window
# ===========================================================================
class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.demo = demo
        self.setWindowTitle(APP_TITLE + ("  —  demonstration mode" if demo else ""))
        self.resize(1180, 820)

        self.store = E.SecretStore()
        try:
            self.config = E.AppConfig.load()
        except E.EngineError as exc:
            QMessageBox.warning(self, APP_TITLE,
                                f"{exc}\n\nStarting with the standard settings instead.")
            self.config = E.AppConfig()

        self.worker: EngineWorker | None = None
        self.one_shot: OneShot | None = None
        self.last_files: list[Path] = []
        self.log_path = E.setup_logging(False)

        self.tabs = QTabWidget()
        self.tab_config = ConfigTab(self.config, self.store)
        self.tab_run = RunTab()
        self.tab_data = DataTab()
        self.tabs.addTab(self.tab_config, "Settings")
        self.tabs.addTab(self.tab_run, "Run")
        self.tabs.addTab(self.tab_data, "Results")
        self.setCentralWidget(self.tabs)

        self.bridge = QtLogBridge()
        self.bridge.record.connect(self.tab_run.append_log)
        log.addHandler(self.bridge)

        self._build_menu()
        self._connect()
        self.statusBar().showMessage(f"Settings: {E.config_path()}")

        if demo:
            self.tab_run.show_notice(
                "Demonstration mode: the app is replaying profiles saved on this "
                "computer. Nothing is sent to LinkedIn and no sign-in is needed.", "info")
        if not E.browser_installed() and not demo:
            self.tab_run.show_notice(
                "The browser this app uses has not been downloaded yet. It will be "
                "fetched automatically the first time you press Start (about 150 MB, "
                "one time only).", "info")

    # -- wiring ------------------------------------------------------------
    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        save = QAction("&Save settings", self)
        save.setShortcut(QKeySequence.Save)
        save.triggered.connect(self.save_settings)
        file_menu.addAction(save)
        reload_action = QAction("Re&load settings", self)
        reload_action.triggered.connect(self.reload_settings)
        file_menu.addAction(reload_action)
        reset = QAction("&Reset to standard settings", self)
        reset.triggered.connect(self.reset_settings)
        file_menu.addAction(reset)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        tools = self.menuBar().addMenu("&Tools")
        for label, slot in (
            ("Open the settings folder", lambda: self._open(E.app_dir())),
            ("Open the log folder", lambda: self._open(E.log_dir())),
            ("Clear saved profiles", self.clear_cache),
            ("Check this computer (diagnostics)", self.show_diagnostics),
        ):
            action = QAction(label, self)
            action.triggered.connect(slot)
            tools.addAction(action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About / how this works", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def _connect(self) -> None:
        self.tab_config.testRequested.connect(self.test_input)
        self.tab_config.signInRequested.connect(self.sign_in_only)
        self.tab_run.startRequested.connect(self.start_run)
        self.tab_run.pauseRequested.connect(lambda: self.worker and self.worker.request_pause())
        self.tab_run.resumeRequested.connect(lambda: self.worker and self.worker.request_resume())
        self.tab_run.stopRequested.connect(self.stop_run)
        self.tab_run.openFolderRequested.connect(
            lambda: self._open(Path(self.config.output_folder)))
        self.tab_data.exportRequested.connect(self.export)
        for key, counter in self.tab_run.counters.items():
            if key in ("review", "errors"):
                counter.setCursor(Qt.PointingHandCursor)

    # -- settings ----------------------------------------------------------
    def save_settings(self) -> bool:
        cfg = self.tab_config.apply_to_config()
        problems = cfg.validate()
        if problems:
            QMessageBox.warning(self, APP_TITLE, "Please fix these first:\n\n"
                                + "\n".join(f"  •  {p}" for p in problems))
            return False
        creds = self.tab_config.credentials()
        if cfg.remember_password and creds.usable:
            if not self.store.save(creds.username, creds.password):
                QMessageBox.information(
                    self, APP_TITLE,
                    "Your settings were saved, but the password could not be stored "
                    "on this computer. You will be asked to sign in by hand.")
        cfg.save()
        self.statusBar().showMessage("Settings saved.", 4000)
        return True

    def reload_settings(self) -> None:
        try:
            self.config = E.AppConfig.load()
        except E.EngineError as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.tab_config.config = self.config
        self.tab_config.load_from_config()
        self.statusBar().showMessage("Settings reloaded.", 4000)

    def reset_settings(self) -> None:
        if QMessageBox.question(
                self, APP_TITLE,
                "Put every setting back to the standard values?\n\n"
                "Your columns will go back to the usual nine plus the status columns.",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.config = E.AppConfig()
        self.tab_config.config = self.config
        self.tab_config.load_from_config()

    def clear_cache(self) -> None:
        count = E.RecordCache().clear()
        QMessageBox.information(self, APP_TITLE,
                                f"Removed {count} saved profile(s). The next run will "
                                f"fetch everything fresh from LinkedIn.")

    # -- reading the input -------------------------------------------------
    def test_input(self) -> None:
        cfg = self.tab_config.apply_to_config()
        problems = [p for p in cfg.validate() if "column" not in p.lower()]
        if problems:
            QMessageBox.warning(self, APP_TITLE, "\n".join(problems))
            return
        self.tab_config.label_input_status.setText("Reading...")
        self.tab_config.label_input_status.setStyleSheet(f"color: {COL_MUTED};")
        self.tab_config.button_test.setEnabled(False)
        self.tab_config.button_match.setEnabled(False)

        adopt = self.sender() is self.tab_config.button_match

        def work():
            if cfg.input_mode == "urls":
                rows, headers = E.rows_from_urls(cfg.urls)
            else:
                path = (E.download_gsheet(cfg.sheet_url) if cfg.input_mode == "sheet"
                        else Path(cfg.file_path).expanduser())
                rows, headers = E.load_rows(path)
            return rows, headers

        def done(result):
            rows, headers = result
            self.tab_config.button_test.setEnabled(True)
            self.tab_config.button_match.setEnabled(cfg.input_mode != "urls")
            self.tab_config.set_row_count(len(rows))
            if adopt and headers:
                self.tab_config.adopt_headers(headers)
            else:
                self.tab_config.label_input_status.setText(
                    f"Read {len(rows)} candidates. Columns found: "
                    + ", ".join(headers[:10]) + ("..." if len(headers) > 10 else ""))
                self.tab_config.label_input_status.setStyleSheet(f"color: {COL_GREEN};")

        def failed(message):
            self.tab_config.button_test.setEnabled(True)
            self.tab_config.button_match.setEnabled(cfg.input_mode != "urls")
            self.tab_config.label_input_status.setText("Could not read it.")
            self.tab_config.label_input_status.setStyleSheet(f"color: {COL_RED_LINE};")
            QMessageBox.warning(self, APP_TITLE, message)

        self.one_shot = OneShot(work, self)
        self.one_shot.done.connect(done)
        self.one_shot.failed.connect(failed)
        self.one_shot.start()

    def sign_in_only(self) -> None:
        if self.demo:
            QMessageBox.information(self, APP_TITLE,
                                    "Demonstration mode does not sign in to LinkedIn.")
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, APP_TITLE, "Please wait for the run to finish.")
            return
        creds = self.tab_config.credentials()
        QMessageBox.information(
            self, APP_TITLE,
            "A browser window will open.\n\n"
            "If your details are saved they will be filled in for you. Finish anything "
            "LinkedIn asks for — a code, or a puzzle — and then you can close "
            "this message. The window closes itself once you are in.")

        def work():
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                return E.assisted_login(pw, creds, E.Control(),
                                        lambda ev: log.info("%s", ev.message))

        def done(ok):
            QMessageBox.information(
                self, APP_TITLE,
                "You are signed in. The app will remember this for a few weeks."
                if ok else
                "The sign-in did not complete. You can try again, or leave it and the "
                "app will ask when you press Start.")

        self.one_shot = OneShot(work, self)
        self.one_shot.done.connect(done)
        self.one_shot.failed.connect(
            lambda message: QMessageBox.warning(self, APP_TITLE, message))
        self.one_shot.start()

    # -- the run -----------------------------------------------------------
    def start_run(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        if not self.save_settings():
            return
        cfg = self.config
        warnings = cfg.warnings()
        if warnings:
            text = "\n\n".join(f"•  {w}" for w in warnings)
            if QMessageBox.question(
                    self, APP_TITLE, f"{text}\n\nStart anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
                return

        self.tab_run.hide_notice()
        self.tab_run.log_view.clear()
        self.log_path = E.setup_logging(False, None)
        log.addHandler(self.bridge)
        self.tabs.setCurrentWidget(self.tab_run)

        creds = self.tab_config.credentials()
        self.worker = EngineWorker(cfg, creds if creds.usable else None, self.demo, self)
        worker = self.worker
        worker.stateChanged.connect(lambda state: self.tab_run.set_state(state))
        worker.planReady.connect(self._plan_ready)
        worker.rowStarted.connect(self._row_started)
        worker.rowProgress.connect(self._row_progress)
        worker.rowFinished.connect(self._row_finished)
        worker.countersChanged.connect(self._counters)
        worker.etaChanged.connect(self._eta)
        worker.loginNeeded.connect(
            lambda message: self.tab_run.show_notice(message, "warn"))
        worker.loginResult.connect(self._login_result)
        worker.blocked.connect(self._blocked)
        worker.runFinished.connect(self._run_finished)
        worker.failed.connect(self._run_failed)
        worker.start()

    def stop_run(self) -> None:
        if not self.worker or not self.worker.isRunning():
            return
        if QMessageBox.question(
                self, APP_TITLE,
                "Stop the run?\n\nEverything collected so far is kept, and the next run "
                "will carry on from here rather than starting again.",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.worker.request_stop()

    def _plan_ready(self, plan) -> None:
        self.tab_data.prepare(list(plan.headers))
        self.tab_run.progress.setRange(0, plan.total)
        self.tab_run.progress.setValue(0)
        self.tab_config.set_row_count(plan.total)
        pieces = [f"{plan.total} candidates",
                  f"{plan.with_url} with a LinkedIn link"]
        if plan.need_search:
            pieces.append(f"{plan.need_search} to look up by name")
        if plan.already_complete:
            pieces.append(f"{plan.already_complete} already complete")
        self.tab_run.label_current.setText("  ·  ".join(pieces))
        self.tab_run.label_eta.setText(
            f"About {human_time(plan.est_seconds)}, opening roughly "
            f"{plan.est_pageviews:g} LinkedIn pages per candidate.")
        if plan.unreadable_links:
            self.tab_run.show_notice(
                f"{len(plan.unreadable_links)} LinkedIn value(s) could not be read and "
                f"will be looked up by name instead: "
                + ", ".join(plan.unreadable_links[:3])
                + ("..." if len(plan.unreadable_links) > 3 else ""), "warn")

    def _row_started(self, index: int, name: str) -> None:
        total = self.tab_run.progress.maximum()
        self.tab_run.label_current.setText(f"[{index + 1} of {total}]  {name}")

    def _row_progress(self, index: int, stage: str) -> None:
        text = self.tab_run.label_current.text().split("  —  ")[0]
        self.tab_run.label_current.setText(f"{text}  —  {stage}")

    def _row_finished(self, index: int, result) -> None:
        self.tab_data.add_result(result)
        self.tab_run.progress.setValue(self.tab_run.progress.value() + 1)

    def _counters(self, counters: dict) -> None:
        for key, counter in self.tab_run.counters.items():
            counter.set(counters.get(key, 0))

    def _eta(self, seconds: float) -> None:
        if seconds > 0:
            self.tab_run.label_eta.setText(f"About {human_time(seconds)} left.")
        elif seconds == 0:
            self.tab_run.label_eta.setText("Almost done.")

    def _login_result(self, ok: bool, message: str) -> None:
        if ok:
            self.tab_run.hide_notice()
        else:
            self.tab_run.show_notice(
                f"The sign-in did not finish ({message}). Nothing has been collected. "
                f"Press Start when you are ready to try again.", "error")

    def _blocked(self, kind: str) -> None:
        self.tab_run.show_notice(
            f"LinkedIn has paused this session ({kind}). Everything collected so far is "
            f"saved. Please leave it an hour or so, then press Start again — the "
            f"profiles already collected will not be fetched a second time.", "error")

    def _run_finished(self, summary) -> None:
        self.last_files = list(summary.files)
        self.tab_run.button_folder.setEnabled(bool(summary.files))
        counters = summary.counters
        parts = [f"{counters.get('filled', 0)} rows filled in",
                 f"{counters.get('skipped', 0)} skipped",
                 f"{counters.get('not_found', 0)} not found",
                 f"{counters.get('errors', 0)} problems",
                 f"{counters.get('pageviews', 0)} LinkedIn pages opened"]
        kind = "info"
        if summary.reason.startswith("blocked"):
            kind = "error"
        elif summary.reason == "stopped":
            kind = "warn"
        where = "\n".join(str(f) for f in summary.files)
        self.tab_run.show_notice(
            f"Finished — {', '.join(parts)}.\n\nSaved to:\n{where}", kind)
        self.tab_run.label_eta.setText("")
        self.tab_data.refresh_summary()
        if self.tab_data.model.rowCount():
            # Show everything. Jumping straight to the "to check" filter hides most
            # of the rows and makes a good run look like a failed one.
            self.tab_data.focus_filter("all")
            self.tabs.setCurrentWidget(self.tab_data)

    def _run_failed(self, kind: str, message: str) -> None:
        self.tab_run.show_notice(message, "error")
        QMessageBox.warning(self, APP_TITLE, message)

    # -- export ------------------------------------------------------------
    def export(self) -> None:
        model = self.tab_data.model
        if not model.rowCount():
            QMessageBox.information(self, APP_TITLE, "There is nothing to export yet.")
            return
        default = str(Path(self.config.output_folder) / f"{self.config.output_stem}.xlsx")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the results", default,
            "Excel workbook (*.xlsx);;CSV file (*.csv)")
        if not path:
            return
        target = Path(path)
        rows = [dict(r) for r in model.rows]
        headers = list(model.headers)
        review_header = next((c.header for c in self.config.columns
                              if c.field == "needs_review"), "Needs Review")
        want_xlsx = target.suffix.lower() != ".csv"

        def work():
            return E.write_outputs(rows, headers, target.parent, target.stem,
                                   want_xlsx=want_xlsx, want_csv=not want_xlsx,
                                   review_header=review_header,
                                   highlight=self.config.highlight_needs_review)

        self.setCursor(Qt.WaitCursor)
        self.one_shot = OneShot(work, self)
        self.one_shot.done.connect(self._export_done)
        self.one_shot.failed.connect(self._export_failed)
        self.one_shot.start()

    def _export_done(self, files) -> None:
        self.unsetCursor()
        self.statusBar().showMessage(f"Exported to {files[0]}", 6000)
        QMessageBox.information(self, APP_TITLE,
                                "Saved:\n" + "\n".join(str(f) for f in files))

    def _export_failed(self, message: str) -> None:
        self.unsetCursor()
        QMessageBox.warning(self, APP_TITLE, message)

    # -- helpers -----------------------------------------------------------
    def _open(self, path: Path) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_diagnostics(self) -> None:
        lines = diagnostics_text().splitlines()
        self._show_report(lines)

    def _show_report(self, lines: list[str]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Diagnostics")
        dialog.resize(760, 480)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("If you need help, copy this and send it along:"))
        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setStyleSheet("font-family: Menlo, Consolas, monospace;")
        layout.addWidget(text)
        row = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(lines)))
        row.addWidget(copy)
        save = QPushButton("Save to a file")
        save.clicked.connect(lambda: self._save_report("\n".join(lines)))
        row.addWidget(save)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        row.addWidget(close)
        layout.addLayout(row)
        dialog.exec()

    def _save_report(self, text: str) -> None:
        path = E.app_dir() / "diagnostics.txt"
        path.write_text(text, encoding="utf-8")
        QMessageBox.information(self, APP_TITLE, f"Saved to\n{path}")

    def show_about(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("About " + APP_TITLE)
        dialog.resize(700, 540)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(f"""
        <h2>{APP_TITLE}</h2>
        <p>Fills in the blanks in a candidate list from LinkedIn.</p>

        <h3>How to use it</h3>
        <ol>
          <li><b>Settings</b> &mdash; point it at your Google Sheet or file, enter your
              LinkedIn details, and choose what each column should contain.</li>
          <li><b>Run</b> &mdash; press Start. The first time, a browser window opens so
              you can sign in.</li>
          <li><b>Results</b> &mdash; check the shaded rows, correct anything by
              double-clicking, then Export.</li>
        </ol>

        <h3>Things worth knowing</h3>
        <p><b>Email and phone are usually blank.</b> LinkedIn only shows them when the
        person has chosen to publish them, which most people have not. The columns that
        fill in reliably are the colleges, degrees and companies.</p>

        <p><b>Finding people by name is a guess.</b> LinkedIn cannot be searched by email
        or phone number. When there is no link, the app searches the name and tells you
        how confident it is. A common or one-word name can easily match the wrong
        person, so those rows are always flagged for you to check.</p>

        <p><b>Existing values are never overwritten.</b> Only empty cells are filled, so
        anything you have corrected by hand stays put.</p>

        <p><b>Go gently.</b> Collecting profile data automatically is against LinkedIn's
        User Agreement, and opening a lot of profiles quickly can get an account
        restricted. The app works through candidates one at a time with a pause between
        each, and only opens the pages your chosen columns actually need. Please keep it
        that way.</p>

        <p>You are responsible for the personal data you collect. Use it only for
        candidates who expect to be considered, and in line with your own privacy
        obligations.</p>

        <p style="color:{COL_MUTED}">Settings and logs: {E.app_dir()}</p>
        """)
        layout.addWidget(browser)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignRight)
        dialog.exec()

    # -- shutdown ----------------------------------------------------------
    def closeEvent(self, event) -> None:
        worker = self.worker
        if worker is None or not worker.isRunning():
            log.removeHandler(self.bridge)
            event.accept()
            return
        if QMessageBox.question(
                self, APP_TITLE,
                "A run is still going. Stop it and close?\n\n"
                "Everything collected so far is kept.",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            event.ignore()
            return
        # Never terminate the thread: that would strand a Chrome process holding the
        # browser profile's lock and the next run could not start.  Ask it to stop,
        # then close once it has actually finished.
        worker.request_stop()
        self.statusBar().showMessage("Closing the browser, one moment...")
        worker.finished.connect(self.close)
        event.ignore()


# ===========================================================================
def diagnostics_text() -> str:
    """A copy-and-send report. The only way to see inside a frozen build on a
    machine you do not have, so it must never need the interface to be working."""
    lines = [f"{APP_TITLE} diagnostics", "=" * 32]
    def add(label, value):
        lines.append(f"{label:<22}{value}")
    add("App version", E.ENGINE_VERSION)
    add("Python", sys.version.split()[0])
    add("Platform", sys.platform)
    add("Frozen build", getattr(sys, "frozen", False))
    if getattr(sys, "frozen", False):
        add("Bundle folder", getattr(sys, "_MEIPASS", "?"))
    try:
        add("Settings folder", E.app_dir())
        add("Settings file", f"{E.config_path()} "
                             f"({'exists' if E.config_path().exists() else 'not yet'})")
        add("Browser profile", E.profile_dir())
        add("Saved profiles", f"{len(list(E.cache_dir().glob('*.json')))} in {E.cache_dir()}")
        add("Log folder", E.log_dir())
    except Exception as exc:
        add("Folders", f"PROBLEM: {exc}")
    try:
        import playwright
        add("Playwright", getattr(playwright, "__version__", "installed"))
    except Exception as exc:
        add("Playwright", f"NOT AVAILABLE: {exc}")
    add("Browser found", E.find_chrome_binary() or "not yet -- it downloads on first run")
    try:
        store = E.SecretStore()
        add("Password storage", store.name if store.available
            else f"none ({store.reason})")
    except Exception as exc:
        add("Password storage", f"PROBLEM: {exc}")
    try:
        cfg = E.AppConfig.load()
        problems = cfg.validate()
        add("Columns enabled", len(cfg.enabled_fields))
        add("Pages per profile", f"{len(cfg.needs)} ({LF.describe_visits(cfg.needs)})")
        add("Settings valid", "yes" if not problems else "; ".join(problems))
    except Exception as exc:
        add("Settings", f"PROBLEM: {exc}")
    add("Fields available", len(LF.FIELD_LIST))
    try:
        from PySide6 import __version__ as qt_version
        add("Qt (PySide6)", qt_version)
    except Exception as exc:
        add("Qt (PySide6)", f"PROBLEM: {exc}")
    return "\n".join(lines)


def run_doctor() -> int:
    """--doctor: print the report and save it, so it is usable even in a windowed
    build where nothing is printed to a console."""
    report = diagnostics_text()
    print(report)
    try:
        path = E.app_dir() / "diagnostics.txt"
        path.write_text(report, encoding="utf-8")
        print(f"\nSaved to {path}")
    except Exception as exc:
        print(f"\nCould not save the report: {exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--doctor" in argv:
        return run_doctor()
    if "--version" in argv:
        print(f"{APP_TITLE} {E.ENGINE_VERSION}")
        return 0
    demo = "--demo" in argv
    argv = [a for a in argv if a != "--demo"]

    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName(ORG_NAME)
    app.setStyle("Fusion")

    window = MainWindow(demo=demo)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
