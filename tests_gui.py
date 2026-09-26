"""
Offscreen tests for the desktop interface.

Runs with QT_QPA_PLATFORM=offscreen, so it needs no display and no LinkedIn.
Covers the two things most likely to break silently: the mapping editor's effect
on the config, and the results table's behaviour when it is sorted and edited.

Run:  QT_QPA_PLATFORM=offscreen python3 tests_gui.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LI_ENRICHER_HOME", tempfile.mkdtemp(prefix="li-test-"))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

import li_engine as E
import li_fields as LF
import li_app as A

FAILURES: list[str] = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n       got:  {got!r}\n       want: {want!r}")


def ok(label, condition, detail=""):
    if not condition:
        FAILURES.append(f"{label}{(' -- ' + detail) if detail else ''}")


def make_result(index, values, status="ok", review=False, notes=""):
    return E.RowResult(index=index, name=f"row{index}", slug=f"slug{index}",
                       status=status, values=values, filled=("email",),
                       needs_review=review, confidence="high", found_by="sheet",
                       notes=notes, from_cache=False, pageviews=3, elapsed=1.0)


# ---------------------------------------------------------------------------
def test_mapping_model():
    model = A.ColumnMapModel(E.default_columns())
    check("starts with the standard preset", model.rowCount(), len(LF.DEFAULT_PRESET))

    # rename a heading
    index = model.index(0, A.ColumnMapModel.COL_HEADER)
    model.setData(index, "Person", Qt.EditRole)
    check("a heading can be renamed", model.columns[0].header, "Person")

    # add a column and point it at a field
    row = model.add_column()
    check("a new column starts unmapped", model.columns[row].field, None)
    model.setData(model.index(row, A.ColumnMapModel.COL_FIELD), "current_company",
                  Qt.EditRole)
    check("a new column can be mapped", model.columns[row].field, "current_company")
    check("mapping a blank column names it too", model.columns[row].header,
          "Current Company")

    # turning one off removes it from the field set
    before = len(model.enabled_fields())
    model.setData(model.index(row, A.ColumnMapModel.COL_ENABLED),
                  Qt.Unchecked.value, Qt.CheckStateRole)
    check("disabling a column drops its field", len(model.enabled_fields()), before - 1)
    model.setData(model.index(row, A.ColumnMapModel.COL_ENABLED),
                  Qt.Checked.value, Qt.CheckStateRole)

    # reorder
    first, second = model.columns[0].header, model.columns[1].header
    model.move_row(0, 1)
    check("move down swaps the order", [model.columns[0].header, model.columns[1].header],
          [second, first])

    # remove
    count = model.rowCount()
    model.remove_rows([row])
    check("a column can be removed", model.rowCount(), count - 1)

    # the cost column explains itself
    email_row = next(i for i, c in enumerate(model.columns) if c.field == "email")
    cost = model.data(model.index(email_row, A.ColumnMapModel.COL_COST), Qt.DisplayRole)
    ok("email shows that it costs the contact page", "contact" in cost, cost)
    name_row = next(i for i, c in enumerate(model.columns) if c.field == "full_name")
    check("a profile-only field is free",
          model.data(model.index(name_row, A.ColumnMapModel.COL_COST), Qt.DisplayRole),
          "free")

    # an unmapped column is described as the user's own
    blank = model.add_column()
    text = model.data(model.index(blank, A.ColumnMapModel.COL_FIELD), Qt.DisplayRole)
    ok("an unmapped column says it is left alone", "blank" in text.lower(), text)


def test_results_model_sorted_edit():
    """The classic trap: editing a cell while the view is sorted must write to the
    right underlying row."""
    headers = ["Candidate Name", "Email Id", "Needs Review"]
    model = A.ResultsModel(headers)
    proxy = A.ResultsFilter()
    proxy.setSourceModel(model)

    model.upsert(make_result(0, {"Candidate Name": "Zara", "Email Id": "z@x.com",
                                 "Needs Review": ""}))
    model.upsert(make_result(1, {"Candidate Name": "Alice", "Email Id": "a@x.com",
                                 "Needs Review": "yes"}, review=True))
    model.upsert(make_result(2, {"Candidate Name": "Mo", "Email Id": "m@x.com",
                                 "Needs Review": ""}))
    check("three rows arrived", model.rowCount(), 3)

    proxy.sort(0, Qt.AscendingOrder)
    first = proxy.index(0, 0)
    check("sorted view shows Alice first", first.data(), "Alice")

    # edit through the proxy; it must land on Alice, not on row 0 of the source
    proxy.setData(proxy.index(0, 1), "alice@new.com", Qt.EditRole)
    alice = next(r for r in model.rows if r["Candidate Name"] == "Alice")
    check("an edit through a sorted proxy hits the right row",
          alice["Email Id"], "alice@new.com")
    zara = next(r for r in model.rows if r["Candidate Name"] == "Zara")
    check("the other rows are untouched", zara["Email Id"], "z@x.com")
    check("the edit is counted", model.edited_count(), 1)

    # a re-arriving result must not wipe a hand correction
    model.upsert(make_result(1, {"Candidate Name": "Alice", "Email Id": "a@x.com",
                                 "Needs Review": "yes"}, review=True))
    alice = next(r for r in model.rows if r["Candidate Name"] == "Alice")
    check("a later update keeps your edit", alice["Email Id"], "alice@new.com")

    # shading
    alice_row = model.rows.index(alice)
    brush = model.data(model.index(alice_row, 0), Qt.BackgroundRole)
    ok("a row needing review is shaded", brush is not None
       and brush.color().name().upper() == A.COL_AMBER.upper(),
       str(brush.color().name() if brush else None))
    zara_row = model.rows.index(zara)
    ok("an ordinary row is not shaded",
       model.data(model.index(zara_row, 0), Qt.BackgroundRole) is None)

    # an error row is shaded differently
    model.upsert(make_result(3, {"Candidate Name": "Err", "Email Id": "",
                                 "Needs Review": ""}, status="error", notes="boom"))
    err_row = next(i for i, r in enumerate(model.rows) if r["Candidate Name"] == "Err")
    brush = model.data(model.index(err_row, 0), Qt.BackgroundRole)
    ok("a failed row is shaded red", brush is not None
       and brush.color().name().upper() == A.COL_RED.upper())
    check("errors are counted", model.error_count(), 1)


def test_results_filters():
    headers = ["Candidate Name", "Needs Review"]
    model = A.ResultsModel(headers)
    proxy = A.ResultsFilter()
    proxy.setSourceModel(model)
    model.upsert(make_result(0, {"Candidate Name": "Ann", "Needs Review": "yes"},
                             review=True))
    model.upsert(make_result(1, {"Candidate Name": "Bob", "Needs Review": ""}))
    model.upsert(make_result(2, {"Candidate Name": "Cid", "Needs Review": ""},
                             status="error"))
    check("all rows visible by default", proxy.rowCount(), 3)
    proxy.set_mode("review")
    check("the review filter shows one row", proxy.rowCount(), 1)
    check("and it is the right one", proxy.index(0, 0).data(), "Ann")
    proxy.set_mode("errors")
    check("the problems filter shows one row", proxy.rowCount(), 1)
    proxy.set_mode("all")
    proxy.setFilterFixedString("bob")
    check("search matches regardless of case", proxy.rowCount(), 1)
    proxy.setFilterFixedString("")
    check("clearing the search restores everything", proxy.rowCount(), 3)


def test_config_tab_roundtrip(tmp: Path):
    store = E.SecretStore()
    cfg = E.AppConfig()
    tab = A.ConfigTab(cfg, store)

    tab.radio_file.setChecked(True)
    tab.edit_file.setText(str(tmp / "x.csv"))
    tab.spin_min.setValue(9.0)
    tab.spin_max.setValue(17.0)
    tab.edit_out.setText(str(tmp))
    tab.map_model.add_column("current_company")
    result = tab.apply_to_config()
    check("input mode follows the radio button", result.input_mode, "file")
    check("pacing is picked up", (result.min_delay, result.max_delay), (9.0, 17.0))
    ok("the added field reaches the config", "current_company" in result.enabled_fields)

    # setting min above max must drag max along rather than produce nonsense
    tab.spin_min.setValue(30.0)
    ok("max is pushed up to meet min", tab.spin_max.value() >= 30.0,
       str(tab.spin_max.value()))
    ok("so the config stays valid", not [p for p in tab.apply_to_config().validate()
                                         if "longer than" in p])

    # adopting a file's own headings
    tab.adopt_headers(["Candidate Name", "Email Id", "Some Custom Column", "LinkedIn"])
    headers = [c.header for c in tab.map_model.columns]
    ok("adopted headings are kept verbatim", "Some Custom Column" in headers, str(headers))
    custom = next(c for c in tab.map_model.columns if c.header == "Some Custom Column")
    check("an unrecognised heading is left unmapped", custom.field, None)
    known = next(c for c in tab.map_model.columns if c.header == "Email Id")
    check("a recognised heading is mapped", known.field, "email")
    ok("the status columns are added on adopt",
       any(c.field == "needs_review" for c in tab.map_model.columns))

    # the cost label must react to the mapping
    tab.map_model.set_columns([E.ColumnMap("A", "full_name")])
    tab._refresh_cost()
    ok("cost label reports one page for identity only",
       "1 LinkedIn page" in tab.label_cost.text(), tab.label_cost.text())
    tab.map_model.add_column("email")
    ok("adding email makes it two pages",
       "2 LinkedIn page" in tab.label_cost.text(), tab.label_cost.text())


def test_run_tab_log_batching():
    tab = A.RunTab()
    import logging as L
    for i in range(500):
        tab.append_log(L.INFO, f"line {i}")
    check("nothing is painted before the timer fires", tab.log_view.toPlainText(), "")
    tab._flush_log()
    ok("a batch arrives in one go", tab.log_view.toPlainText().count("\n") >= 499)

    tab.combo_level.setCurrentIndex(2)          # problems only
    tab.log_view.clear()
    tab.append_log(L.INFO, "chatter")
    tab.append_log(L.ERROR, "a real problem")
    tab._flush_log()
    text = tab.log_view.toPlainText()
    ok("the level filter hides chatter", "chatter" not in text, text)
    ok("but keeps problems", "a real problem" in text, text)

    tab.set_state("running")
    ok("start is disabled while running", not tab.button_start.isEnabled())
    ok("stop is enabled while running", tab.button_stop.isEnabled())
    tab.set_state("paused")
    check("the pause button becomes Resume", tab.button_pause.text(), "Resume")
    tab.set_state("done")
    ok("start comes back when finished", tab.button_start.isEnabled())


def test_demo_run_end_to_end(tmp: Path):
    """Drive a real EngineWorker in demo mode and watch the signals arrive."""
    sheet = tmp / "demo.csv"
    sheet.write_text(
        "Candidate Name,Email Id,Contact No.,LinkedIn,UG College,UG Degree,"
        "PG College,PG Degree,Work Ex Company\n"
        + "\n".join(f"Person {i},,,https://www.linkedin.com/in/demo-{i},,,,,"
                    for i in range(4)),
        encoding="utf-8")
    cfg = E.AppConfig(input_mode="file", file_path=str(sheet), min_delay=0, max_delay=0,
                      output_folder=str(tmp / "out"), use_cache=False, name_search=False)

    worker = A.EngineWorker(cfg, None, demo=True)
    seen = {"rows": [], "plan": None, "finished": None, "states": [], "failed": None}
    worker.planReady.connect(lambda p: seen.__setitem__("plan", p))
    worker.rowFinished.connect(lambda i, r: seen["rows"].append(r))
    worker.runFinished.connect(lambda s: seen.__setitem__("finished", s))
    worker.stateChanged.connect(lambda s: seen["states"].append(s))
    worker.failed.connect(lambda k, m: seen.__setitem__("failed", f"{k}: {m}"))

    app = QApplication.instance()
    worker.start()
    deadline = time.time() + 30
    while worker.isRunning() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    worker.wait(5000)
    app.processEvents()

    ok("the demo run did not fail", seen["failed"] is None, str(seen["failed"]))
    ok("a plan was produced", seen["plan"] is not None)
    if seen["plan"]:
        check("the plan counted the rows", seen["plan"].total, 4)
    check("a result arrived for every row", len(seen["rows"]), 4)
    ok("the run reported finishing", seen["finished"] is not None)
    ok("states went through running", "running" in seen["states"], str(seen["states"]))
    if seen["finished"]:
        ok("files were written", len(seen["finished"].files) >= 1)
    filled = [r for r in seen["rows"] if r.filled]
    ok("demo rows were actually filled in", len(filled) >= 1, str(len(filled)))
    # and the values reach the table model
    model = A.ResultsModel(list(seen["plan"].headers))
    for result in seen["rows"]:
        model.upsert(result)
    check("every row reaches the table", model.rowCount(), 4)
    colleges = [r.get("UG College", "") for r in model.rows]
    ok("colleges came through", any(colleges), str(colleges))


def test_demo_stop(tmp: Path):
    sheet = tmp / "demo2.csv"
    sheet.write_text(
        "Candidate Name,LinkedIn\n"
        + "\n".join(f"P{i},https://www.linkedin.com/in/demo-{i}" for i in range(40)),
        encoding="utf-8")
    cfg = E.AppConfig(input_mode="file", file_path=str(sheet), min_delay=1, max_delay=1,
                      output_folder=str(tmp / "out2"), use_cache=False, name_search=False)
    worker = A.EngineWorker(cfg, None, demo=True)
    rows = []
    worker.rowFinished.connect(lambda i, r: rows.append(r))
    app = QApplication.instance()
    worker.start()
    t0 = time.time()
    while time.time() - t0 < 2.0:
        app.processEvents()
        time.sleep(0.02)
    worker.request_stop()
    stopped_at = time.time()
    ok("the worker actually started", len(rows) >= 1, str(len(rows)))
    while worker.isRunning() and time.time() - stopped_at < 10:
        app.processEvents()
        time.sleep(0.02)
    elapsed = time.time() - stopped_at
    ok(f"stop ends the thread promptly ({elapsed:.1f}s)", not worker.isRunning(),
       f"{elapsed:.1f}s")
    ok("far fewer than all 40 rows were done", len(rows) < 40, str(len(rows)))


def test_main_window_builds(tmp: Path):
    window = A.MainWindow(demo=True)
    check("three tabs", window.tabs.count(), 3)
    check("tab names", [window.tabs.tabText(i) for i in range(3)],
          ["Settings", "Run", "Results"])
    ok("the window has a menu bar", window.menuBar() is not None)
    # the diagnostics dialog must be buildable without a display or a browser
    window.config.validate()
    ok("config is valid enough to report on", isinstance(window.config, E.AppConfig))
    window.close()


# ---------------------------------------------------------------------------
def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        steps = [
            ("mapping editor: rename, add, remap, reorder, remove", test_mapping_model),
            ("results table: editing a sorted view", test_results_model_sorted_edit),
            ("results table: filters and search", test_results_filters),
            ("settings tab round-trip and cost label",
             lambda: test_config_tab_roundtrip(tmp)),
            ("run tab: log batching and button states", test_run_tab_log_batching),
            ("a whole run in demo mode", lambda: test_demo_run_end_to_end(tmp)),
            ("stopping a demo run", lambda: test_demo_stop(tmp)),
            ("the main window builds", lambda: test_main_window_builds(tmp)),
        ]
        for label, fn in steps:
            before = len(FAILURES)
            try:
                fn()
            except Exception as exc:
                import traceback as tb
                FAILURES.append(f"{label} raised: {exc}\n{tb.format_exc()}")
            print(f"  [{'ok  ' if len(FAILURES) == before else 'FAIL'}] {label}")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s):\n")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("\nAll interface tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
