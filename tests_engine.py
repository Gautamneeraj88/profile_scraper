"""
Offline tests for the enricher engine.

Everything here runs with no browser and no network.  The 37 real profiles in
out/*.json are the fixture corpus; a FakePage replays them through the genuine
runner so the whole state machine -- pacing, pause, stop, blocks, merging,
export -- is exercised in milliseconds.

Run:  python3 tests_engine.py
"""

from __future__ import annotations

import json
import glob
import re
import threading
import time
from pathlib import Path

import li_fields as LF
import li_engine as E
from li_fields import Ctx, FIELDS

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n       got:  {got!r}\n       want: {want!r}")


def ok(label, condition, detail=""):
    if not condition:
        FAILURES.append(f"{label}{(' -- ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def fixture_records() -> dict[str, dict]:
    """Normalise the old {candidate, profile} fixture shape into a record.

    26 of 37 carry `education.full.entries` alongside `entries`; flatten that once
    here so every test sees the same shape the live scraper now produces.
    """
    out = {}
    for path in sorted((ROOT / "out").glob("*.json")):
        raw = json.loads(path.read_text())
        prof = raw.get("profile") or {}
        for kind in ("education", "experience"):
            sec = prof.get(kind) or {}
            if (sec.get("full") or {}).get("entries"):
                sec["entries"] = sec["full"]["entries"]
        rec = {"slug": path.stem, "url": E.profile_url(path.stem), "ok": True,
               "blocked": None, "profile": prof,
               "education": prof.get("education") or {},
               "experience": prof.get("experience") or {},
               "contact": raw.get("contact") or {}}
        out[path.stem] = rec
    return out


CONTACT_FIXTURES = {
    "full": {"found": True, "email": "a.person@example.com", "phone": "+91 98765 43210",
             "websites": ["https://example.com", "https://example.com"],
             "twitter": "aperson", "text": "Email\na.person@example.com\nPhone\n+91 98765 43210"},
    "email_only": {"found": True, "email": "solo@example.com", "phone": "",
                   "websites": [], "twitter": "", "text": "Email\nsolo@example.com"},
    "absent": {"found": False},
    "bad_phone": {"found": True, "email": "", "phone": "+91 12", "websites": [],
                  "twitter": "", "text": "Phone\n+91 12"},
}


# ---------------------------------------------------------------------------
# a fake browser
# ---------------------------------------------------------------------------
class FakePage:
    """Replays fixtures based on the last URL it was told to open."""

    def __init__(self, records: dict[str, dict], block_at: int | None = None,
                 search_hits: dict | None = None):
        self.records = records
        self.block_at = block_at
        self.search_hits = search_hits or {}
        self.url = "about:blank"
        self.gotos: list[str] = []
        self.default_timeout = 0

    # -- playwright surface ------------------------------------------------
    def set_default_timeout(self, ms): self.default_timeout = ms

    def goto(self, url, **kw):
        self.gotos.append(url)
        if self.block_at is not None and len(self.gotos) >= self.block_at:
            self.url = "https://www.linkedin.com/authwall"
            return
        self.url = url

    def wait_for_timeout(self, ms): pass
    def wait_for_selector(self, sel, **kw): return object()
    def query_selector(self, sel):
        return object() if "global-nav" in sel or "Search" in sel else None
    def bring_to_front(self): pass
    def fill(self, *a, **k): pass
    def click(self, *a, **k): pass
    def type(self, *a, **k): pass

    def evaluate(self, script, arg=None):
        if "__sc" in script or "scrollingElement" in script:
            return {"top": 900, "sh": 900, "ch": 900, "len": 100}
        if "document.body ? document.body.innerText" in script:
            return "Feed"
        if script is E.PROFILE_EXTRACT:
            return self._profile()
        if script is E.DETAIL_EXTRACT:
            return self._detail(arg)
        if script is E.CONTACT_EXTRACT:
            return self._contact()
        if script is E.SEARCH_EXTRACT:
            return {"results": self.search_hits.get("results", []), "blocked": False}
        return None

    # -- payloads ----------------------------------------------------------
    def _slug(self):
        m = re.search(r"/in/([^/?#]+)", self.url)
        return m.group(1) if m else ""

    def _rec(self):
        return self.records.get(self._slug()) or {}

    def _profile(self):
        prof = dict((self._rec().get("profile") or {}))
        prof["sections"] = {
            LF.VISIT_EDUCATION: self._rec().get("education") or {"entries": [], "show_all": None},
            LF.VISIT_EXPERIENCE: self._rec().get("experience") or {"entries": [], "show_all": None},
        }
        prof.setdefault("authwall", False)
        return prof

    def _detail(self, arg):
        anchor = (arg or {}).get("anchor") or ""
        kind = "education" if "school" in anchor else "experience"
        return {"entries": (self._rec().get(kind) or {}).get("entries") or [],
                "url": self.url}

    def _contact(self):
        return self._rec().get("contact") or {"found": False}


class FakeCtxObj:
    def __init__(self, page): self.pages = [page]
    def cookies(self, *a): return [{"name": "li_at", "value": "x"}]
    def close(self): pass
    def new_page(self): return self.pages[0]


def fake_session(runner, page):
    """Swap a real Session for one backed by FakePage."""
    class S:
        def __init__(self):
            self.page = page
            self.relogins = 0
        def open(self): pass
        def close(self): pass
        def live(self): return True
        def relogin(self, emit): return False
    runner._session = S()
    runner._pw = object()
    return runner


# ---------------------------------------------------------------------------
# 1. registry integrity + every extractor over every fixture
# ---------------------------------------------------------------------------
def test_registry(records):
    keys = [s.key for s in LF.FIELD_LIST]
    check("registry: keys unique", len(keys), len(set(keys)))
    for spec in LF.FIELD_LIST:
        ok(f"registry: {spec.key} needs are known visits",
           set(spec.needs) <= LF.ALL_VISITS, str(spec.needs))
        ok(f"registry: {spec.key} in a known group", spec.group in LF.GROUPS, spec.group)
        ok(f"registry: {spec.key} has a label", bool(spec.label))

    empties = [{}, {"profile": None}, {"profile": {}}, {"profile": {"topcard": []}},
               {"contact": None, "profile": {"education": None, "experience": None}}]
    for blank in empties:
        ctx = Ctx(blank, {})
        for spec in LF.FIELD_LIST:
            try:
                value = spec.extract(ctx)
            except Exception as exc:
                FAILURES.append(f"extractor {spec.key} raised on empty record: {exc}")
                continue
            ok(f"extractor {spec.key} returns str on empty", isinstance(value, str))

    for slug, rec in records.items():
        ctx = Ctx(rec, {})
        for spec in LF.FIELD_LIST:
            try:
                value = spec.extract(ctx)
            except Exception as exc:
                FAILURES.append(f"extractor {spec.key} raised on {slug}: {exc}")
                continue
            if not isinstance(value, str):
                FAILURES.append(f"extractor {spec.key} on {slug} returned {type(value)}")
            elif "None" == value.strip():
                FAILURES.append(f"extractor {spec.key} on {slug} leaked the text 'None'")


# ---------------------------------------------------------------------------
# 2. the known-good values that must never regress
# ---------------------------------------------------------------------------
def test_golden(records):
    expected = {
        "aanchalsahoo": {
            "ug_college": "Shri Ram College of Commerce (SRCC)", "ug_degree": "BCom",
            "pg_college": "INSEAD", "pg_degree": "MBA",
            "all_companies": "Bain & Company, JPMorgan Chase & Co., JSW Steel, The Money Roller",
            "location": "France", "current_company": "Bain & Company",
        },
        "aneesh-dubey-44291811a": {
            "ug_college": "Vellore Institute of Technology", "ug_degree": "BTech",
            "pg_college": "Indian School of Business", "pg_degree": "PGP",
            "all_companies": "YCP Auctus, Deloitte", "current_company": "YCP Auctus",
            "current_title": "Management Consultant",
        },
        # the hard one: a duplicate KIIT entry, a 12th-grade entry to ignore, and
        # three grouped-company experience cards
        "abhishek-abh": {
            "ug_college": "Kalinga Institute of Industrial Technology, Bhubaneswar",
            "ug_degree": "BTech", "pg_college": "NYU Stern School of Business",
            "pg_degree": "MBA",
            "all_companies": "Anaqua, Salesforce, Deloitte, DXC Technology, "
                             "Delhi Metro Rail Corporation (DMRC)",
            "school_12th": "Delhi Public School Vasant Kunj",
            "highest_qualification": "MBA", "current_company": "Anaqua",
        },
    }
    for slug, wants in expected.items():
        if slug not in records:
            FAILURES.append(f"golden fixture missing: {slug}")
            continue
        ctx = Ctx(records[slug], {})
        for key, want in wants.items():
            check(f"{slug}.{key}", FIELDS[key].extract(ctx), want)


def test_no_school_in_college(records):
    leak = re.compile(r"public school|high school|vidyalaya|class x|cbse|icse|"
                      r"senior secondary", re.I)
    for slug, rec in records.items():
        ctx = Ctx(rec, {})
        for key in ("ug_college", "pg_college"):
            value = FIELDS[key].extract(ctx)
            if value and leak.search(value):
                FAILURES.append(f"{slug}.{key} is a school, not a college: {value!r}")


def test_headline_never_chrome(records):
    """The old heuristic returned 'He/Him' on 7 of these 37 profiles."""
    for slug, rec in records.items():
        ctx = Ctx(rec, {})
        headline = FIELDS["headline"].extract(ctx)
        if LF.PRONOUN_RE.match(headline.strip()):
            FAILURES.append(f"{slug}: headline is a pronoun: {headline!r}")
        if LF.BADGE_RE.match(headline.strip()):
            FAILURES.append(f"{slug}: headline is a connection badge: {headline!r}")
        ok(f"{slug}: headline is non-empty", bool(headline.strip()))


def test_contact_fields():
    full = Ctx({"contact": CONTACT_FIXTURES["full"]}, {})
    check("contact.email", FIELDS["email"].extract(full), "a.person@example.com")
    check("contact.phone", FIELDS["phone"].extract(full), "+91 98765 43210")
    check("contact.websites dedupes", FIELDS["websites"].extract(full),
          "https://example.com")
    check("contact.twitter", FIELDS["twitter"].extract(full), "aperson")

    absent = Ctx({"contact": CONTACT_FIXTURES["absent"]}, {})
    check("contact absent -> blank email", FIELDS["email"].extract(absent), "")
    bad = Ctx({"contact": CONTACT_FIXTURES["bad_phone"]}, {})
    check("a 2-digit 'phone' is rejected", FIELDS["phone"].extract(bad), "")


# ---------------------------------------------------------------------------
# 3. slug parsing, visit planning, config
# ---------------------------------------------------------------------------
def test_slugs():
    cases = {
        "www.linkedin.com/in/abhishek-abh": "abhishek-abh",
        "linkedin.com/in/indrajeetthigale": "indrajeetthigale",
        "https://www.linkedin.com/in/phanikeerthi-macherla/": "phanikeerthi-macherla",
        "https://www.linkedin.com/in/sarbottam-pal?utm_source=share_via&utm_content="
        "profile&utm_medium=member_android": "sarbottam-pal",
        "https://in.linkedin.com/in/aneesh-dubey-44291811a": "aneesh-dubey-44291811a",
        "https://www.linkedin.com/pub/old-style-profile": "old-style-profile",
        "souvik-kundu-0194a9185": "souvik-kundu-0194a9185",
        "Aditya Thapliyal": None, "#N/A": None, "": None, "  ": None,
        "someone@example.com": None,
        "https://www.linkedin.com/company/bain": None,
        "https://twitter.com/someone": None,
    }
    for raw, want in cases.items():
        check(f"extract_slug({raw[:46]!r})", E.extract_slug(raw), want)


def test_visits():
    check("default preset pages", sorted(LF.required_visits(LF.DEFAULT_PRESET)),
          ["contact", "education", "experience", "profile"])
    check("only current company", sorted(LF.required_visits(["current_company"])),
          ["experience", "profile"])
    check("identity only", sorted(LF.required_visits(["full_name", "headline", "location"])),
          ["profile"])
    check("counts are free", sorted(LF.required_visits(["skills_count"])), ["profile"])
    check("skills costs a page", sorted(LF.required_visits(["skills"])),
          ["profile", "skills"])
    check("meta needs nothing", sorted(LF.required_visits(["status", "notes"])), [])
    check("describe", LF.describe_visits(LF.required_visits(["current_company"])),
          "main profile, experience")


def test_config(tmp: Path):
    cfg = E.AppConfig()
    check("default columns count", len(cfg.columns), len(LF.DEFAULT_PRESET))
    check("default headers match the GradNext sheet",
          [c.header for c in cfg.columns][:9],
          ["Candidate Name", "Email Id", "Contact No.", "LinkedIn", "UG College",
           "UG Degree", "PG College", "PG Degree", "Work Ex Company"])
    cfg.sheet_url = "https://docs.google.com/spreadsheets/d/ABC/edit"
    path = tmp / "config.json"
    cfg.save(path)
    again = E.AppConfig.load(path)
    check("config round-trips", again.to_json(), cfg.to_json())

    # a config from the future must be refused politely, not crash
    raw = json.loads(path.read_text()); raw["config_version"] = 99
    path.write_text(json.dumps(raw))
    try:
        E.AppConfig.load(path)
        FAILURES.append("a newer config version should be refused")
    except E.EngineError as exc:
        ok("newer config gives a friendly message", "newer version" in str(exc))

    # an unknown field degrades to a manual column rather than exploding
    col = E.ColumnMap.from_json({"header": "X", "field": "no_such_field"})
    check("unknown field -> manual column", col.field, None)

    bad = E.AppConfig(min_delay=20, max_delay=5, sheet_url="x")
    ok("validate catches min>max",
       any("longer than" in p for p in bad.validate()), str(bad.validate()))
    dupes = E.AppConfig(sheet_url="x")
    dupes.columns = [E.ColumnMap("Same", "full_name"), E.ColumnMap("Same", "email")]
    ok("validate catches duplicate headers",
       any("more than once" in p for p in dupes.validate()))


def test_gsheet_urls():
    check("export url", E.gsheet_export_url(
        "https://docs.google.com/spreadsheets/d/ABC123/edit?usp=sharing"),
        "https://docs.google.com/spreadsheets/d/ABC123/export?format=csv&gid=0")
    check("export url with gid", E.gsheet_export_url(
        "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=77"),
        "https://docs.google.com/spreadsheets/d/ABC123/export?format=csv&gid=77")
    try:
        E.gsheet_export_url("https://example.com/foo")
        FAILURES.append("a non-Sheets URL should be refused")
    except E.InputError:
        pass


# ---------------------------------------------------------------------------
# 4. Control: pause freezes the clock, stop is prompt
# ---------------------------------------------------------------------------
def test_control():
    c = E.Control()
    t0 = time.monotonic()
    c.sleep(0.3)
    ok("sleep waits", 0.25 <= time.monotonic() - t0 < 1.0)

    c = E.Control()
    threading.Timer(0.15, c.stop).start()
    t0 = time.monotonic()
    try:
        c.sleep(10.0)
        FAILURES.append("stop during a long sleep should raise Cancelled")
    except E.Cancelled:
        elapsed = time.monotonic() - t0
        ok(f"stop is prompt ({elapsed*1000:.0f}ms)", elapsed < 0.6, f"{elapsed:.2f}s")

    c = E.Control()
    c.pause()
    threading.Timer(0.4, c.resume).start()
    t0 = time.monotonic()
    c.sleep(0.2)
    elapsed = time.monotonic() - t0
    ok(f"pause freezes the countdown ({elapsed:.2f}s)", elapsed >= 0.55, f"{elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 5. the runner, end to end, against the fake browser
# ---------------------------------------------------------------------------
def make_sheet(tmp: Path, records: dict) -> Path:
    slugs = list(records)[:6]
    lines = ["Candidate Name,Email Id,Contact No.,LinkedIn,UG College,UG Degree,"
             "PG College,PG Degree,Work Ex Company"]
    for i, slug in enumerate(slugs):
        name = (records[slug]["profile"].get("name") or slug).replace(",", "")
        # row 2 arrives already complete; row 3 has a curated college to protect
        if i == 1:
            lines.append(f"{name},a@b.com,123456789,https://www.linkedin.com/in/{slug},"
                         f"X College,BTech,Y School,MBA,SomeCo")
        elif i == 2:
            lines.append(f"{name},,,https://www.linkedin.com/in/{slug},"
                         f"HAND CURATED COLLEGE,,,,")
        else:
            lines.append(f"{name},,,https://www.linkedin.com/in/{slug},,,,,")
    path = tmp / "input.csv"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def base_config(tmp: Path, sheet: Path) -> E.AppConfig:
    cfg = E.AppConfig(input_mode="file", file_path=str(sheet),
                      min_delay=0, max_delay=0, use_cache=False,
                      output_folder=str(tmp / "out"), name_search=False)
    return cfg


def test_runner_happy(tmp: Path, records: dict):
    sheet = make_sheet(tmp, records)
    cfg = base_config(tmp, sheet)
    runner = E.EnrichRunner(cfg, E.Control())
    plan = runner.load()
    check("plan total", plan.total, 6)
    check("plan with_url", plan.with_url, 6)
    check("plan already complete", plan.already_complete, 1)
    ok("plan needs contact for the default preset", LF.VISIT_CONTACT in plan.needs)

    page = FakePage(records)
    fake_session(runner, page)
    results = [ev for ev in runner.iter_rows() if isinstance(ev, E.RowResult)]
    check("one result per row", len(results), 6)
    check("counters done", runner.counters["done"], 6)
    check("the complete row is skipped", results[1].status, "skipped")
    check("skipped rows cost no pageviews", results[1].pageviews, 0)

    # the no-overwrite guarantee
    curated = runner.rows[2]
    check("a curated cell is never overwritten", curated["UG College"],
          "HAND CURATED COLLEGE")

    summary = runner.finish()
    check("summary reason", summary.reason, "completed")
    ok("wrote two files", len(summary.files) == 2, str(summary.files))
    xlsx = [f for f in summary.files if f.suffix == ".xlsx"][0]
    from openpyxl import load_workbook
    sheet_obj = load_workbook(xlsx).active
    header_row = [c.value for c in sheet_obj[1]]
    check("xlsx header order follows the mapping", header_row[:4],
          ["Candidate Name", "Email Id", "Contact No.", "LinkedIn"])
    check("xlsx has a row per candidate", sheet_obj.max_row, 7)
    ok("headers include the meta columns", "Enrichment Status" in header_row)


def test_runner_pageview_budget(tmp: Path, records: dict):
    """The whole point of `needs`: fewer mapped fields must mean fewer pages."""
    sheet = make_sheet(tmp, records)

    def run_with(fields):
        cfg = base_config(tmp, sheet)
        cfg.columns = [E.ColumnMap(FIELDS[k].label, k) for k in fields]
        runner = E.EnrichRunner(cfg, E.Control())
        runner.load()
        page = FakePage(records)
        fake_session(runner, page)
        for _ in runner.iter_rows():
            pass
        profile_gotos = [g for g in page.gotos if "/in/" in g]
        return len(profile_gotos), runner.counters["pageviews"]

    identity, _ = run_with(["full_name", "headline", "location"])
    company, _ = run_with(["full_name", "current_company"])
    default, _ = run_with(list(LF.DEFAULT_PRESET))
    ok(f"identity-only is 1 page per profile (saw {identity} for 6 rows)",
       identity <= 6, f"{identity}")
    ok(f"current-company costs no more than identity+details ({company})",
       company <= default, f"{company} vs {default}")
    ok(f"the default preset costs more than identity only "
       f"({default} vs {identity})", default > identity)
    print(f"      pageviews for 6 profiles: identity={identity} "
          f"currentCompany={company} default={default}")


def test_runner_block(tmp: Path, records: dict):
    sheet = make_sheet(tmp, records)
    cfg = base_config(tmp, sheet)
    runner = E.EnrichRunner(cfg, E.Control())
    runner.load()
    page = FakePage(records, block_at=4)
    fake_session(runner, page)
    results = [ev for ev in runner.iter_rows() if isinstance(ev, E.RowResult)]
    ok("a block stops the run", runner.reason.startswith("blocked"), runner.reason)
    ok("rows before the block are kept", len(results) >= 1, str(len(results)))
    summary = runner.finish()
    ok("output is still written after a block", len(summary.files) == 2)


def test_runner_stop(tmp: Path, records: dict):
    sheet = make_sheet(tmp, records)
    cfg = base_config(tmp, sheet)
    cfg.min_delay = cfg.max_delay = 5.0          # a long pacing wait to interrupt
    control = E.Control()
    runner = E.EnrichRunner(cfg, control)
    runner.load()
    fake_session(runner, FakePage(records))
    threading.Timer(0.3, control.stop).start()
    t0 = time.monotonic()
    results = [ev for ev in runner.iter_rows() if isinstance(ev, E.RowResult)]
    elapsed = time.monotonic() - t0
    check("stop sets the reason", runner.reason, "stopped")
    ok(f"stop is prompt even mid-pacing ({elapsed:.2f}s)", elapsed < 2.0, f"{elapsed:.2f}s")
    ok("fewer rows are processed than the sheet holds", len(results) < 6, str(len(results)))
    # the row that was in flight is deliberately dropped -- it was not finished --
    # but everything already completed must still reach the file.
    summary = runner.finish()
    ok("output is written after a stop", len(summary.files) == 2)
    from openpyxl import load_workbook
    xlsx = [f for f in summary.files if f.suffix == ".xlsx"][0]
    ok("every input row still appears in the output",
       load_workbook(xlsx).active.max_row == 7)

    # stopping after some rows have finished must keep them
    cfg2 = base_config(tmp, sheet)
    cfg2.min_delay = cfg2.max_delay = 2.0
    control2 = E.Control()
    runner2 = E.EnrichRunner(cfg2, control2)
    runner2.load()
    fake_session(runner2, FakePage(records))
    threading.Timer(2.5, control2.stop).start()
    kept = [ev for ev in runner2.iter_rows() if isinstance(ev, E.RowResult)]
    ok("completed rows survive a later stop", len(kept) >= 1, str(len(kept)))


def test_runner_row_error(tmp: Path, records: dict):
    sheet = make_sheet(tmp, records)
    cfg = base_config(tmp, sheet)
    runner = E.EnrichRunner(cfg, E.Control())
    runner.load()
    page = FakePage(records)
    original = page.evaluate
    state = {"n": 0}

    def flaky(script, arg=None):
        if script is E.PROFILE_EXTRACT:
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("simulated page failure")
        return original(script, arg)

    page.evaluate = flaky
    fake_session(runner, page)
    results = [ev for ev in runner.iter_rows() if isinstance(ev, E.RowResult)]
    check("one failure does not end the run", len(results), 6)
    ok("the failure is recorded as an error",
       any(r.status == "error" for r in results))
    ok("the error message reaches the notes",
       any("simulated page failure" in (r.notes or "") for r in results))


def test_runner_name_search(tmp: Path, records: dict):
    slug = "aneesh-dubey-44291811a"
    path = tmp / "search.csv"
    path.write_text("Candidate Name,Email Id,Contact No.,LinkedIn,UG College,UG Degree,"
                    "PG College,PG Degree,Work Ex Company\n"
                    "Aneesh Dubey,aneesh@example.com,,,,,,,\n"
                    "Urvi,urvi@example.com,,,,,,,\n", encoding="utf-8")
    cfg = base_config(tmp, path)
    cfg.name_search = True
    runner = E.EnrichRunner(cfg, E.Control())
    plan = runner.load()
    check("both rows need a search", plan.need_search, 2)

    page = FakePage(records, search_hits={"results": [
        {"slug": slug, "lines": ["Aneesh Dubey", "Management Consultant",
                                 "Delhi, India", "2nd"]}]})
    fake_session(runner, page)
    results = [ev for ev in runner.iter_rows() if isinstance(ev, E.RowResult)]
    check("search result count", len(results), 2)
    first = results[0]
    check("a good name match is high confidence", first.confidence, "high")
    check("found_by records the search", first.found_by, "name search")
    ok("the found link is written",
       runner.rows[0]["LinkedIn"].endswith(f"/in/{slug}/"), runner.rows[0]["LinkedIn"])
    second = results[1]
    ok("a one-word name is never high confidence", second.confidence != "high",
       second.confidence)
    ok("an unmatched row is flagged for review",
       second.needs_review or second.status == "not_found", second.status)


def test_cache_rules(tmp: Path, records: dict):
    import li_engine
    original = li_engine.cache_dir
    cache_path = tmp / "cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    li_engine.cache_dir = lambda: cache_path
    try:
        cache = E.RecordCache(ttl_days=14, enabled=True)
        slug = "aneesh-dubey-44291811a"
        rec = dict(records[slug]); rec["ok"] = True
        edu_only = frozenset({LF.VISIT_PROFILE, LF.VISIT_EDUCATION})
        cache.put(slug, edu_only, rec, 2)

        got, missing = cache.get(slug, edu_only)
        ok("an exact-needs cache entry is a hit", got is not None and not missing)

        got, missing = cache.get(slug, edu_only | {LF.VISIT_CONTACT})
        ok("a record without contact does not satisfy a contact run",
           got is not None and LF.VISIT_CONTACT in missing, str(missing))

        # a schema bump must invalidate, not silently reuse
        path = cache._path(slug)
        raw = json.loads(path.read_text()); raw["schema"] = 0
        path.write_text(json.dumps(raw))
        got, missing = cache.get(slug, edu_only)
        ok("an older cache schema is refetched", got is None)

        raw["schema"] = LF.RECORD_SCHEMA
        raw["fetched_at"] = time.time() - 40 * 86400
        path.write_text(json.dumps(raw))
        got, _ = cache.get(slug, edu_only)
        ok("an expired cache entry is refetched", got is None)
    finally:
        li_engine.cache_dir = original


def test_rows_from_urls():
    rows, headers = E.rows_from_urls([
        "https://www.linkedin.com/in/abc", "linkedin.com/in/def   www.linkedin.com/in/ghi",
        "https://www.linkedin.com/in/abc", "not a link"])
    check("url mode header", headers, ["LinkedIn"])
    check("url mode dedupes and parses", [r["LinkedIn"] for r in rows],
          ["https://www.linkedin.com/in/abc/", "https://www.linkedin.com/in/def/",
           "https://www.linkedin.com/in/ghi/"])
    try:
        E.rows_from_urls(["nothing useful"])
        FAILURES.append("a url list with no links should be refused")
    except E.InputError:
        pass


def test_logging_idempotent(tmp: Path):
    import li_engine
    original = li_engine.log_dir
    li_engine.log_dir = lambda: tmp
    try:
        E.setup_logging(False)
        E.setup_logging(False)
        E.setup_logging(False)
        owned = [h for h in E.log.handlers if getattr(h, "_enrich_owned", False)]
        check("repeat setup_logging leaves one handler", len(owned), 1)
    finally:
        li_engine.log_dir = original


def test_redaction():
    E.REDACT.add("sup3rsecret-pw")
    record = E.log.makeRecord("enrich", 20, __file__, 1,
                              "tried to fill sup3rsecret-pw", (), None)
    E.REDACT.filter(record)
    ok("the password is redacted from logs", "sup3rsecret-pw" not in record.getMessage(),
       record.getMessage())


# ---------------------------------------------------------------------------
# The groups that replay the real profiles in out/.  Skipped, not failed, when
# that folder is absent -- which it is in the public repository.
CORPUS_GROUPS = {
    "registry and extractor safety",
    "known-good field values",
    "schools never leak into college columns",
    "headline is never a pronoun or badge",
    "runner: a normal run",
    "runner: pageview budget",
    "runner: a LinkedIn block",
    "runner: stop mid-run",
    "runner: one row failing",
    "runner: name search",
    "cache version and top-up rules",
}


def main() -> int:
    import tempfile
    records = fixture_records()
    # out/ holds real scraped profiles, so it is deliberately absent from the
    # public repository.  Without it the corpus-driven groups cannot run, but
    # the ones that need no profiles still can -- so skip rather than fail,
    # and let a fresh clone build.
    if not records:
        print(f"Offline engine tests -- no profile corpus, "
              f"{len(LF.FIELD_LIST)} fields\n")
        print("  Note: out/ is empty, so the groups that replay real profiles\n"
              "        are skipped.  Everything that needs no profile data still runs.\n")
    else:
        print(f"Offline engine tests -- {len(records)} real profiles, "
              f"{len(LF.FIELD_LIST)} fields\n")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # (label, fn, needs_corpus)
        steps = [
            ("registry and extractor safety", lambda: test_registry(records)),
            ("known-good field values", lambda: test_golden(records)),
            ("schools never leak into college columns",
             lambda: test_no_school_in_college(records)),
            ("headline is never a pronoun or badge",
             lambda: test_headline_never_chrome(records)),
            ("contact fields", test_contact_fields),
            ("LinkedIn URL shapes", test_slugs),
            ("page-visit planning", test_visits),
            ("config save/load/validate", lambda: test_config(tmp)),
            ("Google Sheet URLs", test_gsheet_urls),
            ("pause and stop timing", test_control),
            ("runner: a normal run", lambda: test_runner_happy(tmp, records)),
            ("runner: pageview budget", lambda: test_runner_pageview_budget(tmp, records)),
            ("runner: a LinkedIn block", lambda: test_runner_block(tmp, records)),
            ("runner: stop mid-run", lambda: test_runner_stop(tmp, records)),
            ("runner: one row failing", lambda: test_runner_row_error(tmp, records)),
            ("runner: name search", lambda: test_runner_name_search(tmp, records)),
            ("cache version and top-up rules", lambda: test_cache_rules(tmp, records)),
            ("pasted URL input", test_rows_from_urls),
            ("logging is idempotent", lambda: test_logging_idempotent(tmp)),
            ("password redaction", test_redaction),
        ]
        skipped = 0
        for label, fn in steps:
            if not records and label in CORPUS_GROUPS:
                print(f"  [skip] {label}")
                skipped += 1
                continue
            before = len(FAILURES)
            try:
                fn()
            except Exception as exc:
                import traceback as tb
                FAILURES.append(f"{label} raised: {exc}\n{tb.format_exc()}")
            mark = "ok  " if len(FAILURES) == before else "FAIL"
            print(f"  [{mark}] {label}")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s):\n")
        for f in FAILURES:
            print("  - " + f)
        return 1
    if skipped:
        print(f"\nAll {len(steps) - skipped} runnable groups passed; "
              f"{skipped} skipped for want of the profile corpus.")
    else:
        print("\nAll offline engine tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
