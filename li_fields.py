"""
Field catalogue for the LinkedIn Enricher.
=========================================

This module is the single source of truth for "what can be scraped".  It replaces
the five places the old CLI encoded the same nine columns (CANON_COLUMNS,
HEADER_ALIASES, TARGET_KEYS, merge_row's hard-coded tuple, and derive_fields'
closed return dict), so the user can rename, add, remove and remap columns freely.

A `FieldSpec` says four useful things:

* how to get the value out of a scraped record (`extract`)
* which page visits that requires (`needs`) -- so a run opens ONLY the pages the
  user's chosen fields actually need.  Mapping just "Current Company" costs one
  pageview per candidate instead of four.
* what to call the column by default (`label`) and which existing spreadsheet
  headers should auto-map to it (`aliases`)
* whether it may overwrite a value the user already had (`overwrite`)

Extractors receive a `Ctx`, not a raw record.  `Ctx` memoises the expensive
shared work -- education classification, experience flattening, top-card parsing
-- so enabling twenty education columns parses the education section once per
candidate rather than twenty times.

Extractors must never raise.  A blocked, empty or half-scraped record has to
yield "" rather than take down a whole run, so every one of them is exercised
against empty input in the test suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as _dc_field
from datetime import date
from functools import cached_property
from typing import Callable

# ---------------------------------------------------------------------------
# Visit kinds -- what a field needs the browser to have loaded.
# ---------------------------------------------------------------------------
VISIT_PROFILE = "profile"        # the main /in/<slug>/ page -- always fetched
VISIT_EDUCATION = "education"    # /details/education/ when LinkedIn truncated it
VISIT_EXPERIENCE = "experience"  # /details/experience/ when truncated
VISIT_CONTACT = "contact"        # /overlay/contact-info/
VISIT_SKILLS = "skills"
VISIT_CERTS = "certifications"
VISIT_LANGUAGES = "languages"
VISIT_VOLUNTEER = "volunteering"
VISIT_PROJECTS = "projects"
VISIT_PUBLICATIONS = "publications"
VISIT_HONOURS = "honors"
VISIT_COURSES = "courses"
VISIT_ORGS = "organizations"
VISIT_TESTS = "test_scores"

ALL_VISITS = frozenset({
    VISIT_PROFILE, VISIT_EDUCATION, VISIT_EXPERIENCE, VISIT_CONTACT, VISIT_SKILLS,
    VISIT_CERTS, VISIT_LANGUAGES, VISIT_VOLUNTEER, VISIT_PROJECTS,
    VISIT_PUBLICATIONS, VISIT_HONOURS, VISIT_COURSES, VISIT_ORGS, VISIT_TESTS,
})

# Visit order, so page plans read sensibly and deterministically.
VISIT_ORDER = (VISIT_PROFILE, VISIT_EDUCATION, VISIT_EXPERIENCE, VISIT_CONTACT,
               VISIT_SKILLS, VISIT_CERTS, VISIT_LANGUAGES, VISIT_VOLUNTEER,
               VISIT_PROJECTS, VISIT_PUBLICATIONS, VISIT_HONOURS, VISIT_COURSES,
               VISIT_ORGS, VISIT_TESTS)

# The /in/<slug>/details/<path>/ segment for each section.
SECTION_URL = {
    VISIT_EDUCATION: "education", VISIT_EXPERIENCE: "experience",
    VISIT_SKILLS: "skills", VISIT_CERTS: "certifications",
    VISIT_LANGUAGES: "languages", VISIT_VOLUNTEER: "volunteering-experiences",
    VISIT_PROJECTS: "projects", VISIT_PUBLICATIONS: "publications",
    VISIT_HONOURS: "honors", VISIT_COURSES: "courses",
    VISIT_ORGS: "organizations", VISIT_TESTS: "test-scores",
}

# How each section's heading appears on the profile, for the "does this person
# even have this section?" gate.  Skipping absent sections is free and means
# enabling Languages costs an extra pageview on only the ~35% who list any.
SECTION_HEADING = {
    VISIT_EDUCATION: r"^education", VISIT_EXPERIENCE: r"^experience",
    VISIT_SKILLS: r"^skills", VISIT_CERTS: r"^licen[sc]es",
    VISIT_LANGUAGES: r"^languages", VISIT_VOLUNTEER: r"^volunteer",
    VISIT_PROJECTS: r"^projects", VISIT_PUBLICATIONS: r"^publications",
    VISIT_HONOURS: r"^honors?\b|^honours?\b", VISIT_COURSES: r"^courses",
    VISIT_ORGS: r"^organi[sz]ations", VISIT_TESTS: r"^test scores",
}

VISIT_LABELS = {
    VISIT_PROFILE: "main profile", VISIT_EDUCATION: "education",
    VISIT_EXPERIENCE: "experience", VISIT_CONTACT: "contact info",
    VISIT_SKILLS: "skills", VISIT_CERTS: "certifications",
    VISIT_LANGUAGES: "languages", VISIT_VOLUNTEER: "volunteering",
    VISIT_PROJECTS: "projects", VISIT_PUBLICATIONS: "publications",
    VISIT_HONOURS: "honours", VISIT_COURSES: "courses",
    VISIT_ORGS: "organizations", VISIT_TESTS: "test scores",
}

GROUPS = ["Identity", "Contact", "Education", "Experience", "Profile sections", "Meta"]

# Bumped whenever the shape of a scraped record changes, so stale cache entries
# are not silently re-used against a newer extractor.
RECORD_SCHEMA = 3


class Overwrite:
    IF_BLANK = "if_blank"   # default -- never clobber a value the user already has
    ALWAYS = "always"       # meta columns we own outright
    NEVER = "never"         # read-only


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    group: str
    extract: Callable[["Ctx"], str]
    needs: frozenset = _dc_field(default=frozenset({VISIT_PROFILE}))
    aliases: tuple[str, ...] = ()
    overwrite: str = Overwrite.IF_BLANK
    help: str = ""
    experimental: bool = False


# ---------------------------------------------------------------------------
# Top-card parsing.
#
# LinkedIn's top card is a flat list of text lines with no stable markers, but it
# has one reliable landmark: the lone "." separator immediately before
# "Contact info".  Across every profile sampled the shape is
#
#   [name, (pronouns)?, (connection badge)?, headline, location, "·", "Contact info", ...]
#
# so the two lines before that separator are the location and the headline.  The
# previous implementation just took the first line longer than three characters,
# which returned "He/Him" on 7 of 37 real profiles.
# ---------------------------------------------------------------------------
PRONOUN_RE = re.compile(
    r"^(he|she|they|him|her|them)\s*/\s*(him|her|them|his|hers|theirs)$", re.I)
BADGE_RE = re.compile(r"^[·•]?\s*(1st|2nd|3rd|following)$", re.I)
COUNT_RE = re.compile(r"^([\d,]+\+?)$")
FOLLOWERS_RE = re.compile(r"([\d,]+)\s+followers?", re.I)
HEADING_COUNT_RE = re.compile(r"^(.*?)\s*\((\d[\d,]*)\)\s*$")

_CHROME_WORDS = {
    "contact info", "connections", "follow", "following", "message", "connect",
    "more", "show all", "activity", "add profile section", "open to", "enhance profile",
}


def _is_chrome(line: str) -> bool:
    """True for top-card lines that are interface furniture rather than content."""
    s = (line or "").strip()
    return (not s or s in {"·", "•"}
            or PRONOUN_RE.match(s) is not None
            or BADGE_RE.match(s) is not None
            or COUNT_RE.match(s) is not None
            or FOLLOWERS_RE.search(s) is not None
            or s.lower() in _CHROME_WORDS)


@dataclass
class TopCard:
    name: str = ""
    headline: str = ""
    location: str = ""
    pronouns: str = ""
    degree: str = ""
    connections: str = ""
    followers: str = ""


def parse_topcard(lines: list[str]) -> TopCard:
    tc = TopCard()
    clean = [str(x) for x in (lines or []) if str(x).strip()]
    if not clean:
        return tc
    tc.name = clean[0].strip()

    for line in clean[1:6]:
        s = line.strip()
        if not tc.pronouns and PRONOUN_RE.match(s):
            tc.pronouns = s
        m = BADGE_RE.match(s)
        if not tc.degree and m and m.group(1).lower() != "following":
            tc.degree = m.group(1)

    for i, line in enumerate(clean):
        if line.strip().lower() == "connections" and i > 0:
            m = COUNT_RE.match(clean[i - 1].strip())
            if m:
                tc.connections = m.group(1)
                break
    for line in clean:
        m = FOLLOWERS_RE.search(line)
        if m:
            tc.followers = m.group(1)
            break

    # Find the "Contact info" landmark and read backwards.
    anchor = -1
    for i, line in enumerate(clean):
        if line.strip().lower() == "contact info":
            anchor = i - 1 if (i >= 1 and clean[i - 1].strip() in {"·", "•"}) else i
            break

    body = [l.strip() for l in clean[1:anchor] if not _is_chrome(l)] if anchor > 0 else []
    if len(body) >= 2:
        tc.headline, tc.location = body[-2], body[-1]
    elif len(body) == 1:
        tc.headline = body[0]
    else:
        for line in clean[1:]:
            if not _is_chrome(line):
                tc.headline = line.strip()
                break
    return tc


# ---------------------------------------------------------------------------
# Ctx -- one memoised view over a single scraped record.
#
# Every extractor takes this instead of the raw dict, so the costly shared work
# happens once per candidate no matter how many columns the user has mapped.
# ---------------------------------------------------------------------------
class Ctx:
    """Lazily-derived view of one scraped record, plus the run metadata."""

    def __init__(self, rec: dict | None = None, meta: dict | None = None):
        self.rec = rec if isinstance(rec, dict) else {}
        self.meta = meta if isinstance(meta, dict) else {}

    # -- raw sections ------------------------------------------------------
    @cached_property
    def prof(self) -> dict:
        p = self.rec.get("profile")
        return p if isinstance(p, dict) else {}

    @cached_property
    def contact(self) -> dict:
        c = self.rec.get("contact")
        return c if isinstance(c, dict) else {}

    def section_entries(self, section: str) -> list[dict]:
        """Entries for a section, preferring the fuller /details/ list if present."""
        sec = self.prof.get(section)
        if not isinstance(sec, dict):
            sec = self.rec.get(section)
        if not isinstance(sec, dict):
            return []
        full = sec.get("full") or {}
        entries = full.get("entries") or sec.get("entries") or []
        return entries if isinstance(entries, list) else []

    @cached_property
    def headings(self) -> list[str]:
        h = self.prof.get("headings") or []
        return [str(x) for x in h]

    @cached_property
    def section_counts(self) -> dict[str, int]:
        """Counts LinkedIn puts in its own headings, e.g. 'Skills (44)'.

        Free -- no extra pageview -- and doubles as the gate for whether a
        details page is worth opening at all.
        """
        out: dict[str, int] = {}
        for heading in self.headings:
            m = HEADING_COUNT_RE.match(heading.strip())
            if m:
                out[_norm(m.group(1))] = int(m.group(2).replace(",", ""))
            else:
                out.setdefault(_norm(heading), 0)
        return out

    # -- derived -----------------------------------------------------------
    @cached_property
    def topcard(self) -> TopCard:
        return parse_topcard(self.prof.get("topcard") or [])

    @cached_property
    def edu(self) -> list[dict]:
        return classify_education(self.section_entries("education"))

    @cached_property
    def edu_real(self) -> list[dict]:
        return dedupe_education([e for e in self.edu if e["level"] != "school"])

    @cached_property
    def ug_pg(self) -> tuple[dict, dict, set]:
        ug, pg, guessed = pick_ug_pg(self.edu)
        return ug or {}, pg or {}, guessed

    @cached_property
    def roles(self) -> list[dict]:
        return parse_experience(self.section_entries("experience"))

    @cached_property
    def companies(self) -> tuple[str, str]:
        return derive_companies(self.section_entries("experience"))

    @cached_property
    def current_role(self) -> dict:
        for role in self.roles:
            if re.search(r"present", role.get("dates") or "", re.I):
                return role
        return self.roles[0] if self.roles else {}

    def has_section(self, visit: str) -> bool:
        """Did this profile show the section at all? Used to skip pointless visits."""
        pattern = SECTION_HEADING.get(visit)
        if not pattern:
            return False
        return any(re.search(pattern, h.strip(), re.I) for h in self.headings)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# ---------------------------------------------------------------------------
# Education / experience parsing.
#
# Moved here from the original CLI so that all field derivation lives in one
# place.  These regexes were tuned against 37 real Indian + international
# profiles -- treat changes to them as a regression risk.
# ---------------------------------------------------------------------------
PG_RE = re.compile(
    r"\b(mba|pgdm|pgpm?|pgp|post ?graduate|master|m\.?tech|m\.?sc|m\.?s\b|m\.?a\b|m\.?com|"
    r"mca|llm|ph\.?d|doctor|executive (mba|program)|pgdbm|emba)\b", re.I)
UG_RE = re.compile(
    r"\b(bachelor|b\.?tech|b\.?e\b|b\.?sc|b\.?a\b|b\.?com|bba|bca|llb|mbbs|b\.?arch|undergrad)\b",
    re.I)
SCHOOL_RE = re.compile(
    r"(public school|high school|higher secondary|senior secondary|convent|vidyalaya|"
    r"class (x|xii)|hsc|cbse|icse|isc\b|grade 1[02])", re.I)
YEARS_RE = re.compile(
    r"([A-Z][a-z]{2}\s+)?(\d{4})\s*[–—-]\s*(([A-Z][a-z]{2}\s+)?\d{4}|present)", re.I)
DURATION_RE = re.compile(r"\d+\s*(yrs?|mos?)\b", re.I)
WORKMODE_RE = re.compile(r"(on-?site|remote|hybrid)", re.I)
EMPTYPE_RE = re.compile(
    r"(full-?time|part-?time|internship|intern|contract|freelance|self-?employed|"
    r"seasonal|apprenticeship|trainee|permanent)", re.I)
DATE_RANGE_RE = re.compile(
    r"([A-Z][a-z]{2}\s+\d{4}|\d{4})\s*[–—-]\s*(Present|[A-Z][a-z]{2}\s+\d{4}|\d{4})",
    re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

# Longest / most specific first: MBA must win before MA, BTech before BA.
DEGREE_ABBREVIATIONS = [
    (r"\bph\.?\s?d\b|doctor of philosophy", "PhD"),
    (r"\bpgdbm\b", "PGDBM"),
    (r"\bpgdm\b|post ?graduate diploma in management", "PGDM"),
    (r"\bpgpm\b", "PGPM"),
    (r"\bpgp\b|post ?graduate programme? in management", "PGP"),
    (r"\bemba\b|executive mba", "EMBA"),
    (r"\bmba\b|master of business administration", "MBA"),
    (r"\bm\.?tech\b|master of technology", "MTech"),
    (r"\bmca\b", "MCA"),
    (r"\bm\.?com\b|master of commerce", "MCom"),
    (r"\bm\.?sc\b|master of science", "MSc"),
    (r"\bllm\b|master of laws", "LLM"),
    (r"\bm\.?e\b|master of engineering", "ME"),
    (r"\bm\.?s\b", "MS"),
    (r"\bm\.?a\b|master of arts", "MA"),
    (r"\bmbbs\b", "MBBS"),
    (r"\bb\.?tech\b|bachelor of technology", "BTech"),
    (r"\bbba\b|bachelor of business administration", "BBA"),
    (r"\bbca\b", "BCA"),
    (r"\bb\.?com\b|bachelor of commerce", "BCom"),
    (r"\bb\.?sc\b|bachelor of science", "BSc"),
    (r"\bllb\b|bachelor of laws", "LLB"),
    (r"\bb\.?arch\b|bachelor of architecture", "BArch"),
    (r"\bb\.?e\b|bachelor of engineering", "BE"),
    (r"\bb\.?a\b|bachelor of arts", "BA"),
    (r"\bmaster\b", "Masters"),
    (r"\bbachelor\b", "Bachelors"),
]
DEGREE_ABBREVIATIONS = [(re.compile(p, re.I), s) for p, s in DEGREE_ABBREVIATIONS]

# Degree seniority, for "Highest Qualification".
DEGREE_RANK = {"PhD": 5, "LLM": 4, "EMBA": 4, "MBA": 4, "PGDM": 4, "PGDBM": 4, "PGP": 4,
               "PGPM": 4, "MTech": 4, "MSc": 4, "MS": 4, "MCom": 4, "MCA": 4, "MA": 4,
               "ME": 4, "Masters": 4, "MBBS": 3, "BTech": 2, "BE": 2, "BSc": 2, "BCom": 2,
               "BBA": 2, "BCA": 2, "BA": 2, "BArch": 2, "LLB": 2, "Bachelors": 2}

NON_EMPLOYERS = re.compile(r"^(freelance|self.?employed|independent|various|self)$", re.I)
INTERN_RE = re.compile(r"\b(intern|interns|internship|trainee|apprentice\w*)\b", re.I)
SIDE_COURSE_RE = re.compile(
    r"(exchange|semester abroad|study abroad|summer (school|program)|online|"
    r"certificat|bootcamp|nanodegree|workshop)", re.I)
EDU_STOPWORDS = {"the", "of", "and", "at", "for", "a"}

MAX_COMPANIES = 5


def normalise_degree(raw: str) -> str:
    """'Bachelor of Technology - BTech, Mechanical Engineering' -> 'BTech'."""
    if not raw or not str(raw).strip():
        return ""
    head = str(raw).split(",")[0]
    for pat, short in DEGREE_ABBREVIATIONS:
        if pat.search(head):
            return short
    for pat, short in DEGREE_ABBREVIATIONS:
        if pat.search(str(raw)):
            return short
    return head.strip()[:60]


def degree_field(raw: str) -> str:
    """The field of study: everything after the first comma."""
    parts = str(raw or "").split(",", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _end_year(years: str) -> int:
    hits = re.findall(r"\d{4}", years or "")
    if re.search(r"present", years or "", re.I):
        return date.today().year + 1
    return int(hits[-1]) if hits else 0


def _school_key(name: str) -> set[str]:
    toks = re.split(r"[^a-z0-9]+", (name or "").lower())
    return {t for t in toks if len(t) > 1 and t not in EDU_STOPWORDS}


def _edu_score(entry: dict) -> float:
    """Prefer the fuller, dated, primary record when a school appears twice."""
    score = 0.0
    if entry.get("link"):
        score += 2.0
    if re.search(r"\d{4}", entry.get("years") or ""):
        score += 1.0
    if SIDE_COURSE_RE.search(f"{entry.get('school', '')} {entry.get('degree', '')}"):
        score -= 3.0
    return score


def classify_education(entries: list[dict]) -> list[dict]:
    rows = []
    for e in entries or []:
        lines = e.get("lines") or []
        if not lines:
            continue
        school, degree, years = lines[0], "", ""
        for ln in lines[1:]:
            if YEARS_RE.search(ln) or re.fullmatch(r"\d{4}", ln.strip()):
                years = years or ln.strip()
            elif ln.lower().startswith("grade"):
                continue
            elif not degree:
                degree = ln.strip()
        blob = f"{degree} {school}"
        if SCHOOL_RE.search(blob) and not PG_RE.search(blob) and not UG_RE.search(blob):
            level = "school"
        elif PG_RE.search(degree) or (not degree and PG_RE.search(school)):
            level = "pg"
        elif UG_RE.search(degree):
            level = "ug"
        else:
            level = "unknown"
        row = {"school": school, "degree": degree, "years": years, "level": level,
               "end": _end_year(years), "link": e.get("link") or ""}
        row["score"] = _edu_score(row)
        rows.append(row)
    return rows


def dedupe_education(edu: list[dict]) -> list[dict]:
    """Collapse the same school listed twice ('KIIT - Kalinga ...' and 'Kalinga ...').

    Merges only within a level and only on strong token overlap, so
    'Delhi Public School' never swallows 'University of Delhi'.
    """
    kept: list[dict] = []
    for entry in edu:
        key = _school_key(entry["school"])
        match = None
        for other in kept:
            if other["level"] != entry["level"]:
                continue
            okey = _school_key(other["school"])
            if not key or not okey:
                continue
            if len(key & okey) / min(len(key), len(okey)) >= 0.7:
                match = other
                break
        if match is None:
            kept.append(entry)
        elif entry["score"] > match["score"]:
            kept[kept.index(match)] = entry
    return kept


def pick_ug_pg(edu: list[dict]) -> tuple[dict | None, dict | None, set[str]]:
    """Which entry is the undergrad one, which the postgrad one.

    Third value names slots that had to be *guessed* from dates because the degree
    text gave no clue -- the caller flags those for human review.
    """
    real = dedupe_education([e for e in edu if e["level"] != "school"])

    def best(level):
        same = [e for e in real if e["level"] == level]
        return max(same, key=lambda e: (e["score"], e["end"])) if same else None

    ug, pg = best("ug"), best("pg")
    guessed: set[str] = set()
    leftovers = [e for e in real if e["level"] == "unknown"]

    if leftovers:
        by_recency = sorted(leftovers, key=lambda e: e["end"])
        if ug is None and pg is None:
            ug = by_recency[0]
            guessed.add("ug")
            if len(by_recency) > 1:
                pg = by_recency[-1]
                guessed.add("pg")
        elif ug is None:
            older = [e for e in by_recency if not pg or pg["end"] == 0 or e["end"] <= pg["end"]]
            ug = (older or by_recency)[0]
            guessed.add("ug")
        elif pg is None:
            newer = [e for e in by_recency if e["end"] > ug["end"] > 0]
            if newer:
                pg = newer[-1]
                guessed.add("pg")
    return ug, pg, guessed


def parse_experience(entries: list[dict]) -> list[dict]:
    """Flatten the experience list, including LinkedIn's grouped-company layout.

    A card with a duration but no date range is a *company header* whose siblings
    are the individual roles; we remember its name against the /company/ href and
    apply it to those roles.  Without this, multi-role employers lose their name.
    """
    roles, company_by_link = [], {}
    for e in entries or []:
        lines = e.get("lines") or []
        link = e.get("link") or ""
        if not lines:
            continue
        span = None
        for ln in lines:
            if DATE_RANGE_RE.search(ln):
                span = ln
                break
        company, title = "", lines[0]
        for ln in lines[1:2]:
            if DATE_RANGE_RE.search(ln) or DURATION_RE.search(ln):
                continue
            left, _, right = ln.partition("·")
            left, right = left.strip(), right.strip()
            if right and WORKMODE_RE.fullmatch(right):
                continue
            if left and not WORKMODE_RE.fullmatch(left) and not EMPTYPE_RE.fullmatch(left):
                company = left
                break
        is_group = not span and any(DURATION_RE.search(l) for l in lines[1:])
        if is_group:
            company_by_link[link] = lines[0]
            continue
        company = company_by_link.get(link) or company
        if company:
            company_by_link.setdefault(link, company)
        roles.append({"title": title, "company": company, "dates": span or "",
                      "link": link, "intern": bool(INTERN_RE.search(" ".join(lines[:2]))),
                      "duration": next((l for l in lines if DURATION_RE.search(l)), "")})
    return roles


def derive_companies(entries: list[dict], limit: int = MAX_COMPANIES) -> tuple[str, str]:
    """Employers most-recent-first, one name each.  Returns (capped, full).

    Multiple roles at one employer collapse to a single name.  When there are more
    employers than fit, companies where every role was an internship give up their
    place first.
    """
    order: list[str] = []
    info: dict[str, dict] = {}
    for role in parse_experience(entries):
        name = (role.get("company") or "").strip().strip(",;").strip()
        if not name or NON_EMPLOYERS.match(name):
            continue
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if not key:
            continue
        if key not in info:
            info[key] = {"name": name, "all_intern": True}
            order.append(key)
        if not role.get("intern"):
            info[key]["all_intern"] = False

    kept = list(order)
    for key in reversed(order):
        if len(kept) <= limit:
            break
        if info[key]["all_intern"]:
            kept.remove(key)
    return (", ".join(info[k]["name"] for k in kept[:limit]),
            ", ".join(info[k]["name"] for k in order))


def _ym(text: str) -> tuple[int, int] | None:
    text = (text or "").strip()
    if re.fullmatch(r"present", text, re.I):
        today = date.today()
        return (today.year, today.month)
    m = re.fullmatch(r"([A-Z][a-z]{2})\s+(\d{4})", text)
    if m:
        return (int(m.group(2)), MONTHS.get(m.group(1), 1))
    if re.fullmatch(r"\d{4}", text):
        return (int(text), 1)
    return None


def total_experience_months(entries: list[dict]) -> int:
    """Union of role date ranges, so concurrent jobs are not double-counted."""
    spans = []
    for role in parse_experience(entries):
        m = DATE_RANGE_RE.search(role.get("dates") or "")
        if not m:
            continue
        a, b = _ym(m.group(1)), _ym(m.group(2))
        if a and b:
            spans.append((a, b))
    spans.sort()
    merged, cur = [], None
    for start, end in spans:
        if cur and (start[0] * 12 + start[1]) <= (cur[1][0] * 12 + cur[1][1]):
            if (end[0] * 12 + end[1]) > (cur[1][0] * 12 + cur[1][1]):
                cur = (cur[0], end)
        else:
            if cur:
                merged.append(cur)
            cur = (start, end)
    if cur:
        merged.append(cur)
    return sum(max(0, (e[0] - s[0]) * 12 + (e[1] - s[1])) for s, e in merged)

# ---------------------------------------------------------------------------
# Extractors.  All take a Ctx, all return str, none may raise.
# ---------------------------------------------------------------------------
def _join(items, sep: str = ", ", limit: int = 0) -> str:
    out, seen = [], set()
    for item in items:
        s = str(item or "").strip()
        key = s.lower()
        if not s or key in seen:
            continue
        seen.add(key)
        out.append(s)
        if limit and len(out) >= limit:
            break
    return sep.join(out)


# -- Identity ---------------------------------------------------------------
def x_full_name(c: Ctx) -> str:
    return (c.prof.get("name") or c.topcard.name or "").strip()


def x_first_name(c: Ctx) -> str:
    parts = [p for p in x_full_name(c).split() if p.strip(".")]
    return parts[0] if parts else ""


def x_last_name(c: Ctx) -> str:
    parts = [p for p in x_full_name(c).split() if p.strip(".")]
    return parts[-1] if len(parts) > 1 else ""


def x_headline(c: Ctx) -> str:
    direct = (c.prof.get("headline") or "").strip()
    if direct and not _is_chrome(direct):
        return direct
    return c.topcard.headline


def x_location(c: Ctx) -> str:
    return (c.prof.get("location") or "").strip() or c.topcard.location


def x_pronouns(c: Ctx) -> str:
    return c.topcard.pronouns


def x_degree(c: Ctx) -> str:
    return c.topcard.degree


def x_about(c: Ctx) -> str:
    return (c.prof.get("about") or "").strip()


def x_profile_url(c: Ctx) -> str:
    return str(c.rec.get("url") or "").strip()


def x_final_url(c: Ctx) -> str:
    return str(c.rec.get("final_url") or "").strip()


def x_slug(c: Ctx) -> str:
    return str(c.rec.get("slug") or "").strip()


def x_photo(c: Ctx) -> str:
    return (c.prof.get("photo") or "").strip()


def x_connections(c: Ctx) -> str:
    return c.topcard.connections


def x_followers(c: Ctx) -> str:
    return c.topcard.followers


def x_open_to_work(c: Ctx) -> str:
    flag = c.prof.get("open_to_work")
    return "yes" if flag else ""


# -- Contact ----------------------------------------------------------------
def x_email(c: Ctx) -> str:
    return (c.contact.get("email") or "").strip()


def x_phone(c: Ctx) -> str:
    phone = (c.contact.get("phone") or "").strip()
    # fewer than 7 digits is a stray number, not a phone number
    return "" if phone and len(re.sub(r"\D", "", phone)) < 7 else phone


def x_websites(c: Ctx) -> str:
    return _join(c.contact.get("websites") or [])


def x_twitter(c: Ctx) -> str:
    return (c.contact.get("twitter") or "").strip()


# -- Education --------------------------------------------------------------
def x_ug_college(c: Ctx) -> str:
    return c.ug_pg[0].get("school", "")


def x_ug_degree(c: Ctx) -> str:
    return normalise_degree(c.ug_pg[0].get("degree", ""))


def x_ug_field(c: Ctx) -> str:
    return degree_field(c.ug_pg[0].get("degree", ""))


def x_ug_years(c: Ctx) -> str:
    return c.ug_pg[0].get("years", "")


def x_pg_college(c: Ctx) -> str:
    return c.ug_pg[1].get("school", "")


def x_pg_degree(c: Ctx) -> str:
    return normalise_degree(c.ug_pg[1].get("degree", ""))


def x_pg_field(c: Ctx) -> str:
    return degree_field(c.ug_pg[1].get("degree", ""))


def x_pg_years(c: Ctx) -> str:
    return c.ug_pg[1].get("years", "")


def x_highest_qualification(c: Ctx) -> str:
    best, rank = "", -1
    for entry in c.edu_real:
        short = normalise_degree(entry["degree"])
        r = DEGREE_RANK.get(short, 1 if short else 0)
        if r > rank:
            best, rank = short, r
    return best


def x_all_colleges(c: Ctx) -> str:
    return _join(e["school"] for e in c.edu_real)


def x_all_degrees(c: Ctx) -> str:
    return _join(normalise_degree(e["degree"]) for e in c.edu_real)


def x_school_12th(c: Ctx) -> str:
    for entry in c.edu:
        if entry["level"] == "school":
            return entry["school"]
    return ""


def x_education_count(c: Ctx) -> str:
    return str(len(c.edu)) if c.edu else ""


# -- Experience -------------------------------------------------------------
def x_current_title(c: Ctx) -> str:
    return c.current_role.get("title", "")


def x_current_company(c: Ctx) -> str:
    return c.current_role.get("company", "")


def x_current_duration(c: Ctx) -> str:
    text = c.current_role.get("duration") or c.current_role.get("dates") or ""
    m = re.search(r"(\d+\s*(?:yrs?|mos?)(?:\s+\d+\s*(?:yrs?|mos?))?)", text, re.I)
    return m.group(1).strip() if m else ""


def x_all_companies(c: Ctx) -> str:
    return c.companies[0]


def x_all_companies_full(c: Ctx) -> str:
    return c.companies[1]


def x_all_titles(c: Ctx) -> str:
    return _join((r.get("title") or "") for r in c.roles)


def x_first_company(c: Ctx) -> str:
    parts = [p.strip() for p in c.companies[1].split(",") if p.strip()]
    return parts[-1] if parts else ""


def x_first_title(c: Ctx) -> str:
    return (c.roles[-1].get("title", "") if c.roles else "")


def x_total_experience(c: Ctx) -> str:
    months = total_experience_months(c.section_entries("experience"))
    return f"{months / 12:.1f}" if months else ""


def x_job_count(c: Ctx) -> str:
    return str(len(c.roles)) if c.roles else ""


def x_latest_job_dates(c: Ctx) -> str:
    return c.current_role.get("dates", "")


def x_employment_type(c: Ctx) -> str:
    for role in (c.current_role,):
        blob = f"{role.get('dates', '')} {role.get('duration', '')}"
        m = EMPTYPE_RE.search(blob)
        if m:
            return m.group(0)
    return ""


# -- Profile sections -------------------------------------------------------
def _section_items(c: Ctx, visit: str, limit: int = 0) -> list[str]:
    out, seen = [], set()
    for entry in c.section_entries(visit):
        lines = entry.get("lines") or []
        if not lines:
            continue
        item = str(lines[0]).strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
        if limit and len(out) >= limit:
            break
    return out


def _section_extractor(visit: str, limit: int = 0):
    def extract(c: Ctx) -> str:
        return _join(_section_items(c, visit, limit))
    return extract


def _count_extractor(visit: str):
    """Reads the count out of LinkedIn's own heading -- costs no pageview."""
    pattern = SECTION_HEADING.get(visit, "")

    def extract(c: Ctx) -> str:
        for heading, count in c.section_counts.items():
            if re.search(_norm(pattern.lstrip("^")) or "$^", heading):
                if count:
                    return str(count)
        for heading in c.headings:
            m = HEADING_COUNT_RE.match(heading.strip())
            if m and pattern and re.search(pattern, m.group(1).strip(), re.I):
                return m.group(2).replace(",", "")
        items = _section_items(c, visit)
        return str(len(items)) if items else ""
    return extract


# -- Meta -------------------------------------------------------------------
def _meta_extractor(key: str):
    def extract(c: Ctx) -> str:
        value = c.meta.get(key, "")
        return "" if value is None else str(value)
    return extract


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
_PROF = frozenset({VISIT_PROFILE})
_EDU = frozenset({VISIT_EDUCATION})
_EXP = frozenset({VISIT_EXPERIENCE})
_CON = frozenset({VISIT_CONTACT})
_NONE = frozenset()


def _f(key, label, group, extract, needs=_PROF, aliases=(),
       overwrite=Overwrite.IF_BLANK, help="", experimental=False) -> FieldSpec:
    return FieldSpec(key=key, label=label, group=group, extract=extract, needs=needs,
                     aliases=aliases, overwrite=overwrite, help=help,
                     experimental=experimental)


FIELD_LIST: list[FieldSpec] = [
    # -- Identity ----------------------------------------------------------
    _f("full_name", "Candidate Name", "Identity", x_full_name,
       aliases=("candidatename", "name", "fullname", "candidate"),
       overwrite=Overwrite.NEVER,
       help="The person's name. Read-only: this identifies the row, so it is "
            "never overwritten from LinkedIn."),
    _f("first_name", "First Name", "Identity", x_first_name, aliases=("firstname",)),
    _f("last_name", "Last Name", "Identity", x_last_name, aliases=("lastname", "surname")),
    _f("headline", "Headline", "Identity", x_headline,
       aliases=("headline", "designation", "currentposition"),
       help="The one-line description under the name, e.g. 'BCG | Kellogg MBA'."),
    _f("location", "Location", "Identity", x_location,
       aliases=("location", "city", "place", "region", "country"),
       help="Where the profile says they are based."),
    _f("about", "About", "Identity", x_about, aliases=("about", "summary", "bio"),
       help="The free-text About section. Can be long."),
    _f("profile_url", "LinkedIn", "Identity", x_profile_url,
       aliases=("linkedin", "linkedinurl", "linkedinprofile", "linkedinlink",
                "linkedinid", "profile", "profileurl"),
       help="The profile URL, tidied to a standard form."),
    _f("final_url", "Resolved URL", "Identity", x_final_url,
       help="Where the profile URL actually ended up. Catches renamed profiles."),
    _f("profile_slug", "Profile Slug", "Identity", x_slug, aliases=("slug",)),
    _f("photo_url", "Profile Photo URL", "Identity", x_photo,
       aliases=("photo", "photourl", "picture", "image", "dp"),
       help="Direct link to the profile picture. These links expire after a while.",
       experimental=True),
    _f("connections", "Connections", "Identity", x_connections, aliases=("connections",)),
    _f("followers", "Followers", "Identity", x_followers, aliases=("followers",)),
    _f("pronouns", "Pronouns", "Identity", x_pronouns, aliases=("pronouns",)),
    _f("degree_of_connection", "Connection Degree", "Identity", x_degree,
       help="1st, 2nd or 3rd -- how closely you are connected to them."),
    _f("open_to_work", "Open To Work", "Identity", x_open_to_work,
       help="'yes' when the profile shows the Open To Work badge.", experimental=True),

    # -- Contact -----------------------------------------------------------
    _f("email", "Email Id", "Contact", x_email, needs=_CON,
       aliases=("emailid", "email", "emailaddress", "mailid", "emailids"),
       help="Only available when the member published it. Usually blank -- that is "
            "LinkedIn's privacy setting, not a fault."),
    _f("phone", "Contact No.", "Contact", x_phone, needs=_CON,
       aliases=("contactno", "contactnumber", "contact", "phone", "phoneno",
                "phonenumber", "mobile", "mobileno", "mobilenumber"),
       help="Only available when the member published it. Usually blank."),
    _f("websites", "Websites", "Contact", x_websites, needs=_CON,
       aliases=("website", "websites", "portfolio", "link")),
    _f("twitter", "Twitter", "Contact", x_twitter, needs=_CON, aliases=("twitter", "x")),

    # -- Education ---------------------------------------------------------
    _f("ug_college", "UG College", "Education", x_ug_college, needs=_EDU,
       aliases=("ugcollege", "undergraduatecollege", "ugcollegename", "ugschool"),
       help="Undergraduate institution. School and 12th-grade entries are excluded."),
    _f("ug_degree", "UG Degree", "Education", x_ug_degree, needs=_EDU,
       aliases=("ugdegree", "undergraduatedegree", "ugcourse"),
       help="Shortened, e.g. BTech, BCom, BBA."),
    _f("ug_field", "UG Field of Study", "Education", x_ug_field, needs=_EDU,
       aliases=("ugfield", "ugbranch", "ugstream", "ugspecialisation")),
    _f("ug_years", "UG Years", "Education", x_ug_years, needs=_EDU,
       aliases=("ugyears", "ugduration", "ugbatch")),
    _f("pg_college", "PG College", "Education", x_pg_college, needs=_EDU,
       aliases=("pgcollege", "postgraduatecollege", "pgcollegename", "pgschool")),
    _f("pg_degree", "PG Degree", "Education", x_pg_degree, needs=_EDU,
       aliases=("pgdegree", "postgraduatedegree", "pgcourse"),
       help="Shortened, e.g. MBA, PGDM, MS."),
    _f("pg_field", "PG Field of Study", "Education", x_pg_field, needs=_EDU,
       aliases=("pgfield", "pgstream", "pgspecialisation")),
    _f("pg_years", "PG Years", "Education", x_pg_years, needs=_EDU,
       aliases=("pgyears", "pgduration", "pgbatch")),
    _f("highest_qualification", "Highest Qualification", "Education",
       x_highest_qualification, needs=_EDU,
       aliases=("highestqualification", "highestdegree", "qualification")),
    _f("all_colleges", "All Colleges", "Education", x_all_colleges, needs=_EDU,
       aliases=("allcolleges", "colleges", "institutions")),
    _f("all_degrees", "All Degrees", "Education", x_all_degrees, needs=_EDU,
       aliases=("alldegrees", "degrees")),
    _f("school_12th", "School (12th)", "Education", x_school_12th, needs=_EDU,
       aliases=("school", "school12th", "highschool", "schoolname")),
    _f("education_count", "Education Count", "Education", x_education_count, needs=_EDU),

    # -- Experience --------------------------------------------------------
    _f("current_title", "Current Title", "Experience", x_current_title, needs=_EXP,
       aliases=("currenttitle", "currentrole", "currentdesignation", "role", "title")),
    _f("current_company", "Current Company", "Experience", x_current_company, needs=_EXP,
       aliases=("currentcompany", "company", "employer", "currentemployer", "organisation")),
    _f("current_duration", "Current Company Duration", "Experience", x_current_duration,
       needs=_EXP, aliases=("currentduration", "tenure")),
    _f("employment_type", "Employment Type", "Experience", x_employment_type, needs=_EXP,
       aliases=("employmenttype", "jobtype"),
       help="Full-time, Internship, Contract and so on, for the current role."),
    _f("all_companies", "Work Ex Company", "Experience", x_all_companies, needs=_EXP,
       aliases=("workexcompany", "workexcompanies", "workexperience", "workex",
                "companies", "workexcompanyname", "employers"),
       help="Employers most recent first, one name each, up to 5. Several roles at "
            "one employer collapse to a single name."),
    _f("all_companies_full", "All Companies (full list)", "Experience",
       x_all_companies_full, needs=_EXP, help="Every employer, with no cap."),
    _f("all_titles", "All Job Titles", "Experience", x_all_titles, needs=_EXP,
       aliases=("alltitles", "titles", "roles")),
    _f("first_company", "First Company", "Experience", x_first_company, needs=_EXP,
       aliases=("firstcompany",)),
    _f("first_title", "First Job Title", "Experience", x_first_title, needs=_EXP),
    _f("total_experience", "Total Experience (yrs)", "Experience", x_total_experience,
       needs=_EXP,
       aliases=("totalexperience", "experience", "totalexp", "yearsofexperience", "exp"),
       help="Overlapping roles are counted once, not twice."),
    _f("job_count", "Number of Jobs", "Experience", x_job_count, needs=_EXP,
       aliases=("numberofjobs", "jobcount")),
    _f("latest_job_dates", "Latest Job Dates", "Experience", x_latest_job_dates, needs=_EXP),

    # -- Profile sections --------------------------------------------------
    # The *_count fields read LinkedIn's own heading text, so they are free.
    # The list fields need that section's page opened.
    _f("skills_count", "Skill Count", "Profile sections", _count_extractor(VISIT_SKILLS),
       help="Free -- taken from the 'Skills (44)' heading, no extra page opened."),
    _f("top_skills", "Top Skills", "Profile sections",
       _section_extractor(VISIT_SKILLS, 5), needs=frozenset({VISIT_SKILLS}),
       aliases=("topskills", "keyskills"), experimental=True),
    _f("skills", "All Skills", "Profile sections", _section_extractor(VISIT_SKILLS),
       needs=frozenset({VISIT_SKILLS}), aliases=("skills", "allskills"), experimental=True),
    _f("certifications_count", "Certification Count", "Profile sections",
       _count_extractor(VISIT_CERTS)),
    _f("certifications", "Certifications", "Profile sections",
       _section_extractor(VISIT_CERTS), needs=frozenset({VISIT_CERTS}),
       aliases=("certifications", "certificates", "licenses"), experimental=True),
    _f("languages_count", "Language Count", "Profile sections",
       _count_extractor(VISIT_LANGUAGES)),
    _f("languages", "Languages", "Profile sections", _section_extractor(VISIT_LANGUAGES),
       needs=frozenset({VISIT_LANGUAGES}), aliases=("languages", "language"),
       experimental=True),
    _f("volunteering", "Volunteering", "Profile sections",
       _section_extractor(VISIT_VOLUNTEER), needs=frozenset({VISIT_VOLUNTEER}),
       aliases=("volunteering", "volunteer"), experimental=True),
    _f("projects", "Projects", "Profile sections", _section_extractor(VISIT_PROJECTS),
       needs=frozenset({VISIT_PROJECTS}), aliases=("projects",), experimental=True),
    _f("publications", "Publications", "Profile sections",
       _section_extractor(VISIT_PUBLICATIONS), needs=frozenset({VISIT_PUBLICATIONS}),
       aliases=("publications", "papers"), experimental=True),
    _f("honours", "Honours & Awards", "Profile sections",
       _section_extractor(VISIT_HONOURS), needs=frozenset({VISIT_HONOURS}),
       aliases=("honors", "honours", "awards"), experimental=True),
    _f("courses", "Courses", "Profile sections", _section_extractor(VISIT_COURSES),
       needs=frozenset({VISIT_COURSES}), aliases=("courses",), experimental=True),
    _f("organizations", "Organizations", "Profile sections",
       _section_extractor(VISIT_ORGS), needs=frozenset({VISIT_ORGS}),
       aliases=("organizations", "organisations"), experimental=True),
    _f("test_scores", "Test Scores", "Profile sections", _section_extractor(VISIT_TESTS),
       needs=frozenset({VISIT_TESTS}), aliases=("testscores", "scores"), experimental=True),

    # -- Meta --------------------------------------------------------------
    _f("status", "Enrichment Status", "Meta", _meta_extractor("status"), needs=_NONE,
       overwrite=Overwrite.ALWAYS, help="ok, skipped, profile not found, error ..."),
    _f("notes", "Enrichment Notes", "Meta", _meta_extractor("notes"), needs=_NONE,
       overwrite=Overwrite.ALWAYS,
       help="Why something is blank, and anything that had to be guessed."),
    _f("needs_review", "Needs Review", "Meta", _meta_extractor("needs_review"),
       needs=_NONE, overwrite=Overwrite.ALWAYS,
       help="'yes' means check this row by hand before trusting it."),
    _f("confidence", "Match Confidence", "Meta", _meta_extractor("confidence"),
       needs=_NONE, overwrite=Overwrite.ALWAYS,
       help="How sure a name search was: high, medium or low."),
    _f("found_by", "LinkedIn Found By", "Meta", _meta_extractor("found_by"), needs=_NONE,
       overwrite=Overwrite.ALWAYS,
       help="'sheet' if you supplied the link, 'name search' if the app found it."),
    _f("scraped_at", "Scraped At", "Meta", _meta_extractor("scraped_at"), needs=_NONE,
       overwrite=Overwrite.ALWAYS),
    _f("pageviews", "Pages Opened", "Meta", _meta_extractor("pageviews"), needs=_NONE,
       overwrite=Overwrite.ALWAYS,
       help="How many LinkedIn pages this candidate cost."),
    _f("row_number", "Source Row", "Meta", _meta_extractor("row_number"), needs=_NONE,
       overwrite=Overwrite.ALWAYS),
]

FIELDS: dict[str, FieldSpec] = {}
for _spec in FIELD_LIST:
    if _spec.key in FIELDS:
        raise RuntimeError(f"duplicate field key in registry: {_spec.key}")
    FIELDS[_spec.key] = _spec

# The nine columns of the original GradNext sheet plus the meta columns -- the
# default preset, so a first run reproduces the old CLI's output exactly.
DEFAULT_PRESET: list[str] = [
    "full_name", "email", "phone", "profile_url",
    "ug_college", "ug_degree", "pg_college", "pg_degree", "all_companies",
    "found_by", "confidence", "needs_review", "status", "notes",
]


def fields_by_group() -> dict[str, list[FieldSpec]]:
    out: dict[str, list[FieldSpec]] = {g: [] for g in GROUPS}
    for spec in FIELD_LIST:
        out.setdefault(spec.group, []).append(spec)
    return out


def required_visits(enabled_keys) -> frozenset:
    """Which pages a run must open, given the fields the user turned on.

    This is what keeps the tool cheap.  Mapping only 'Current Company' costs one
    pageview per candidate; mapping everything costs seven.
    """
    needs = set()
    for key in enabled_keys:
        spec = FIELDS.get(key)
        if spec:
            needs |= set(spec.needs)
    if needs:
        needs.add(VISIT_PROFILE)     # the main page is always the starting point
    return frozenset(needs)


def describe_visits(visits) -> str:
    return ", ".join(VISIT_LABELS.get(v, v) for v in VISIT_ORDER if v in visits)


def alias_index() -> dict[str, str]:
    """normalised header text -> field key, for auto-matching an existing sheet."""
    index: dict[str, str] = {}
    for spec in FIELD_LIST:
        for text in (spec.label, *spec.aliases):
            index.setdefault(_norm(text), spec.key)
    return index


def match_header_to_field(header: str) -> str | None:
    """Best guess at which field an existing spreadsheet header means."""
    return alias_index().get(_norm(header))


def derive_values(ctx: Ctx, keys) -> dict[str, str]:
    """Run the given extractors over one record.  A broken extractor is logged by
    the caller and skipped -- it must never sink a whole run."""
    out: dict[str, str] = {}
    for key in keys:
        spec = FIELDS.get(key)
        if spec is None:
            continue
        try:
            value = spec.extract(ctx)
        except Exception:
            value = ""
        out[key] = "" if value is None else str(value)
    return out
