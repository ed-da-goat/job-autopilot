"""Synthetic demo database: a lively dashboard with no profile, network, LLM, or keys.

Everything here is invented. Company names are coined (any resemblance to a real
business is coincidental), people are placeholders, URLs live under example.com,
and no email address other than @example.com is ever written. Generation is
deterministic for a given seed; only the timestamps move with the wall clock so
the "last 30 days" views always look current.
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunt import db
from jobhunt.eligibility import load_preferences
from jobhunt.rubric import load_rubric

DEMO_DB_PATH = db.DEFAULT_DB_PATH.parent / "demo.db"

# Target current-stage counts at n_postings=5000; they scale down for small runs.
FUNNEL_PLAN = {
    "suggested": 400,
    "qualified": 8,
    "tailored": 5,
    "ready": 4,
    "applied": 120,
    "replied": 14,
    "interviewing": 5,
    "offer": 1,
    "passed": 70,
    "closed": 8,
}

_PREFIXES = [
    "Northwind", "Lumen", "Quillon", "Tavira", "Brindle", "Oakhollow", "Marrowfield",
    "Zephyra", "Halcyon Reach", "Ironbark", "Kestrel Lane", "Mosswick", "Pinecrest Row",
    "Saltmarsh", "Thistledown", "Umberfield", "Vesper Hill", "Wrenfield", "Yarrowby",
    "Ashgrove", "Bellwether Lane", "Cinderpoint", "Driftwood Row", "Emberlyn", "Fernhollow",
    "Glimmerton", "Hollowbrook", "Inkwell Park", "Juniper Vale", "Kindlewood", "Larkspur Bay",
    "Meridian Fold", "Nimbus Court", "Orchardly", "Plumeria", "Quartzmoor", "Ravenmoor",
    "Sablewick", "Tidewater Fold", "Underhill", "Verdantine", "Willowmere", "Xylem Gate",
    "Yonderfield", "Zinnia Row", "Amberly", "Birchwell", "Copperleaf", "Dovetail Hollow",
]
_SUFFIXES = [
    "Robotics", "Ledger", "Labs", "Systems", "Analytics", "Cloud", "Payments", "Health",
    "Logistics", "Security", "Data", "AI", "Software", "Works", "Networks", "Capital",
    "Energy", "Dynamics",
]
_CATEGORIES = ["ai-infra", "fde-solutions", "fintech", "yc-early", "devtools", "other"]
_PLATFORMS = ["greenhouse", "lever", "ashby", "workday"]
_PLATFORM_WEIGHTS = [38, 22, 28, 12]

_TITLES = [
    ("Software Engineer", 14), ("Software Engineer, New Grad", 6), ("Backend Engineer", 8),
    ("Full Stack Engineer", 8), ("Forward Deployed Engineer", 9), ("AI Engineer", 8),
    ("Applied AI Engineer", 5), ("Solutions Engineer", 7), ("Data Engineer", 6),
    ("Machine Learning Engineer", 5), ("Customer Engineer", 3), ("Deployment Strategist", 2),
    ("Technical Account Manager", 2), ("GTM Engineer", 3), ("Platform Engineer", 4),
    ("Senior Software Engineer", 5), ("Staff Software Engineer", 2),
    ("Engineering Manager", 2), ("Product Designer", 2), ("Account Executive", 2),
    ("Software Engineer Intern", 2),
]
_LOCATIONS = [
    ("San Francisco, CA", 18), ("New York, NY", 16), ("Remote - US", 20), ("Atlanta, GA", 7),
    ("Austin, TX", 6), ("Seattle, WA", 6), ("Boston, MA", 5), ("Chicago, IL", 4),
    ("Denver, CO", 3), ("Los Angeles, CA", 4), ("Remote (US)", 5), ("London, UK", 3),
    ("Toronto, Canada", 3),
]
_SKILLS = [
    "Python", "TypeScript", "SQL", "REST APIs", "Postgres", "React", "Go", "Kubernetes",
    "LLM tooling", "data pipelines", "customer-facing demos", "event-driven systems",
]
_BLURBS = [
    "You will own features end to end, from a rough customer problem to a shipped release.",
    "Work directly with customers to scope, build, and deploy integrations in their environment.",
    "Build the services that sit between our models and the products people use every day.",
    "Join a small team shipping weekly; you will have real ownership from day one.",
    "Help us turn messy operational data into reliable, auditable workflows.",
]

_FIRST_NAMES = ["Avery", "Morgan", "Rowan", "Sage", "Quinn", "Dakota", "Emerson", "Harper"]
_LAST_NAMES = ["Stonebrook", "Fairweather", "Lindqvist-Demo", "Ashcombe", "Marlowe-Demo", "Pemberly"]

_ROUNDS = ["Recruiter screen", "Technical screen", "Take-home review", "Onsite loop", "Hiring manager chat"]
_PASS_REASONS = ["seniority", "geography", "skills", "timing", "not_interested", "duplicate"]


def _wchoice(rng: random.Random, pairs: list[tuple[str, int]]) -> str:
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _is_default_db(path: Path) -> bool:
    return path.expanduser().resolve() == db.DEFAULT_DB_PATH.resolve()


def _make_companies(rng: random.Random, n: int = 150) -> list[dict]:
    names: set[str] = set()
    out: list[dict] = []
    while len(out) < n:
        name = f"{rng.choice(_PREFIXES)} {rng.choice(_SUFFIXES)}"
        if name in names:
            continue
        names.add(name)
        out.append(
            {
                "name": name,
                "slug": _slug(name),
                "platform": rng.choices(_PLATFORMS, weights=_PLATFORM_WEIGHTS)[0],
                "category": rng.choice(_CATEGORIES),
            }
        )
    return out


def _make_score(rng: random.Random, total: int, rubric: dict) -> dict:
    """Split `total` into the rubric's components so they add up exactly."""
    maxes = dict(rubric["components"])
    bonus_max = {"degree_language_bonus": 6, "early_grad_bonus": 6, "finance_hybrid_bonus": 10}
    bonuses = {k: (v if rng.random() < 0.12 else 0) for k, v in bonus_max.items()}
    main_total = max(0, min(sum(maxes.values()), total - sum(bonuses.values())))
    weights = {k: m * (0.55 + 0.9 * rng.random()) for k, m in maxes.items()}
    scale = main_total / sum(weights.values())
    comps = {k: min(maxes[k], int(round(w * scale))) for k, w in weights.items()}
    diff = main_total - sum(comps.values())
    for k in rng.sample(list(comps), len(comps)) * 12:
        if diff == 0:
            break
        step = 1 if diff > 0 else -1
        if 0 <= comps[k] + step <= maxes[k]:
            comps[k] += step
            diff -= step
    return {**comps, **bonuses}


def seed(
    db_path: Path | str,
    n_postings: int = 5000,
    seed: int = 42,  # noqa: A002 - public name required by the CLI contract
) -> dict:
    """Fill `db_path` with synthetic data and return summary counts.

    Refuses the real database path. Deterministic for a given (n_postings, seed)."""
    path = Path(db_path)
    if _is_default_db(path):
        raise ValueError(f"refusing to seed demo data into the real database: {db.DEFAULT_DB_PATH}")

    rng = random.Random(seed)
    now = datetime.now(UTC).replace(microsecond=0)
    rubric = load_rubric()
    rubric_version = int(rubric["version"])
    prefs_version = load_preferences().version
    scale = n_postings / 5000

    conn = db.connect(path)
    try:
        with conn:
            counts = _populate(conn, rng, now, rubric, rubric_version, prefs_version, n_postings, scale)
    finally:
        conn.close()
    return counts


def _populate(conn, rng, now, rubric, rubric_version, prefs_version, n_postings, scale) -> dict:
    companies = _make_companies(rng)
    added = _iso(now - timedelta(days=45))
    conn.executemany(
        "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (c["name"], c["category"], c["platform"], c["slug"], "yes",
             f"Synthetic demo company ({c['category']}).", 1 if rng.random() < 0.08 else 0, added)
            for c in companies
        ],
    )
    for src in _PLATFORMS:
        conn.execute(
            "INSERT OR REPLACE INTO source_state (source, baseline_at, last_success_at, consecutive_failures)"
            " VALUES (?,?,?,0)",
            (src, _iso(now - timedelta(days=30)), _iso(now - timedelta(hours=rng.randint(1, 9)))),
        )
    for day in range(7, 0, -1):
        for src in _PLATFORMS:
            seen = rng.randint(300, 900)
            at = now - timedelta(days=day, hours=rng.randint(0, 3))
            conn.execute(
                "INSERT INTO sweeps (started_at, finished_at, source, boards_attempted, boards_ok,"
                " postings_seen, postings_new, errors) VALUES (?,?,?,?,?,?,?,?)",
                (_iso(at), _iso(at + timedelta(minutes=4)), src, 40, 40, seen, rng.randint(5, 60), "[]"),
            )
    for job in ("daily", "apply-queue", "watch"):
        started = now - timedelta(minutes=rng.randint(15, 90))
        conn.execute(
            "INSERT INTO run_log (job, started_at, finished_at, ok, summary) VALUES (?,?,?,1,?)",
            (job, _iso(started), _iso(started + timedelta(minutes=2)), "demo run: ok"),
        )

    # --- postings ---------------------------------------------------------------------
    posting_rows = []
    meta = []  # parallel to posting_rows
    for i in range(n_postings):
        co = rng.choice(companies)
        title = _wchoice(rng, _TITLES)
        location = _wchoice(rng, _LOCATIONS)
        days_ago = rng.random() ** 1.4 * 30
        posted = now - timedelta(days=days_ago)
        first_seen = posted + timedelta(hours=rng.randint(1, 20))
        first_seen = min(first_seen, now)
        spid = f"{co['slug'][:14]}-{rng.randint(100000, 999999)}-{i}"
        skills = ", ".join(rng.sample(_SKILLS, 3))
        text = (
            f"{co['name']} is hiring a {title} ({location}). {rng.choice(_BLURBS)} "
            f"You will work with {skills}. This is a synthetic posting for demo purposes."
        )
        if co["platform"] == "greenhouse":
            raw = {"content": f"<p>{text}</p>"}
        elif co["platform"] == "lever":
            raw = {"descriptionPlain": text, "lists": []}
        elif co["platform"] == "ashby":
            raw = {"descriptionPlain": text}
        else:
            raw = {"jobPostingInfo": {"title": title}}
        status = "closed" if rng.random() < 0.05 else "active"
        url = f"https://jobs.example.com/{co['platform']}/{co['slug']}/{spid}"
        posting_rows.append(
            (co["platform"], spid, co["name"], title, location, url, _iso(posted),
             json.dumps(raw), _iso(first_seen), _iso(now), status)
        )
        meta.append({"platform": co["platform"], "first_seen": first_seen, "status": status})
    conn.executemany(
        "INSERT INTO postings (source, source_posting_id, company, title, location, url, posted_at,"
        " raw_json, first_seen_at, last_seen_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        posting_rows,
    )
    ids = [r[0] for r in conn.execute("SELECT id FROM postings ORDER BY id")]

    # --- scores + eligibility ----------------------------------------------------------
    totals: list[int | None] = []
    score_rows = []
    elig_rows = []
    for idx, pid in enumerate(ids):
        row = posting_rows[idx]
        title = row[3]
        draw = rng.random()
        excluded = "intern" in title.lower() or rng.random() < 0.03
        if excluded:
            total = None
        elif draw < 0.10:
            total = rng.randint(75, 96)
        elif draw < 0.30:
            total = rng.randint(60, 74)
        else:
            total = int(min(60, max(25, rng.gauss(45, 8))))
        totals.append(total)
        scored_at = _iso(meta[idx]["first_seen"] + timedelta(hours=2))
        if total is None:
            score_rows.append((pid, rubric_version, None, "{}", "{}", "[]", 1,
                               "intern/apprentice title" if "intern" in title.lower() else "not a target role",
                               scored_at))
            elig_rows.append((pid, rubric_version, prefs_version, "blocked",
                              json.dumps(["Excluded by score filter"]), "[]", "[]", scored_at))
            continue
        comps = _make_score(rng, total, rubric)
        evidence = {"seniority_fit": f"Title '{title}' read as {'early-career' if 'Senior' not in title and 'Staff' not in title else 'senior'}."}
        missing = ["no_description"] if meta[idx]["platform"] == "workday" else []
        score_rows.append((pid, rubric_version, total, json.dumps(comps), json.dumps(evidence),
                           json.dumps(missing), 0, None, scored_at))
        loc = row[4]
        if "Senior" in title or "Staff" in title or "Manager" in title:
            status, blockers, warnings = "blocked", ["Blocked seniority title"], []
        elif "UK" in loc or "Canada" in loc:
            status, blockers, warnings = "blocked", ["International-only location"], []
        elif draw >= 0.5 and rng.random() < 0.18:
            status, blockers, warnings = "review", [], ["Remote scope needs review"]
        else:
            status, blockers, warnings = "eligible", [], []
        ev = [f"US location option: {loc}"] if status == "eligible" else []
        elig_rows.append((pid, rubric_version, prefs_version, status, json.dumps(blockers),
                          json.dumps(warnings), json.dumps(ev), scored_at))
    conn.executemany(
        "INSERT INTO scores (posting_id, rubric_version, total, components, score_evidence,"
        " missing_evidence, excluded, exclude_reason, scored_at) VALUES (?,?,?,?,?,?,?,?,?)",
        score_rows,
    )
    conn.executemany(
        "INSERT INTO eligibility_assessments (posting_id, rubric_version, preferences_version, status,"
        " blockers, warnings, evidence, assessed_at) VALUES (?,?,?,?,?,?,?,?)",
        elig_rows,
    )

    # --- funnel ------------------------------------------------------------------------
    elig_status = {r[0]: r[3] for r in elig_rows}
    pool = [
        pid for idx, pid in enumerate(ids)
        if totals[idx] is not None and totals[idx] >= 60 and meta[idx]["status"] == "active"
        and elig_status[pid] == "eligible"
    ]
    rng.shuffle(pool)
    plan = {k: max(1 if k in ("offer", "interviewing") else 0, round(v * scale)) for k, v in FUNNEL_PLAN.items()}
    path_to = {
        "suggested": ["harvested", "suggested"],
        "qualified": ["harvested", "suggested", "qualified"],
        "tailored": ["harvested", "suggested", "qualified", "tailored"],
        "ready": ["harvested", "suggested", "qualified", "tailored", "ready"],
        "applied": ["harvested", "suggested", "qualified", "tailored", "ready", "applied"],
        "replied": ["harvested", "suggested", "qualified", "tailored", "ready", "applied", "replied"],
        "interviewing": ["harvested", "suggested", "qualified", "tailored", "ready", "applied", "replied", "interviewing"],
        "offer": ["harvested", "suggested", "qualified", "tailored", "ready", "applied", "replied", "interviewing", "offer"],
        "passed": ["harvested", "suggested", "passed"],
        "closed": ["harvested", "suggested", "qualified", "tailored", "ready", "applied", "closed"],
    }
    # Hand out the pool from the most-advanced stage down so the best matches get furthest.
    order = ["offer", "interviewing", "replied", "closed", "applied", "ready", "tailored", "qualified",
             "suggested", "passed"]
    idx_of = {pid: i for i, pid in enumerate(ids)}
    cursor = 0
    funnel_rows, event_rows, app_rows, open_events = [], [], [], []
    staged: dict[str, list[int]] = {}
    for stage in order:
        take = pool[cursor:cursor + plan[stage]]
        cursor += len(take)
        staged[stage] = take
    for stage, pids in staged.items():
        for pid in pids:
            i = idx_of[pid]
            chain = path_to[stage]
            t = meta[i]["first_seen"]
            applied_at = None
            created = _iso(t)
            horizon = (now - t).total_seconds()
            # spread the remaining history across [first_seen, now]
            step = max(3600.0, horizon / (len(chain) + 1))
            events = []
            prev = None
            for hop, to_stage in enumerate(chain):
                if hop > 0:
                    t = min(now - timedelta(minutes=rng.randint(1, 600)),
                            t + timedelta(seconds=step * (0.4 + 1.2 * rng.random())))
                note = None
                if to_stage == "suggested":
                    note = f"auto-suggest: score {totals[i]} >= threshold"
                elif to_stage == "passed":
                    note = "passed in demo"
                events.append((pid, prev, to_stage, note, _iso(t)))
                if to_stage == "applied":
                    applied_at = _iso(t)
                prev = to_stage
            event_rows.extend(events)
            updated = events[-1][4]
            next_action, next_date = None, None
            if stage == "applied":
                next_action = "follow up"
                next_date = (datetime.fromisoformat(applied_at) + timedelta(days=7)).date().isoformat()
            elif stage in ("qualified", "tailored", "ready"):
                next_action = {"qualified": "tailor resume", "tailored": "final review", "ready": "submit"}[stage]
                next_date = (now + timedelta(days=rng.randint(0, 3))).date().isoformat()
            elif stage in ("replied", "interviewing"):
                next_action = "reply to recruiter" if stage == "replied" else "prep next round"
                next_date = (now + timedelta(days=rng.randint(0, 4))).date().isoformat()
            pass_reason = rng.choice(_PASS_REASONS) if stage == "passed" else None
            tier = "top20" if (totals[i] or 0) >= 75 else ("fast" if (totals[i] or 0) >= 65 else "stretch")
            funnel_rows.append(
                (pid, stage, tier if stage != "suggested" else None, next_action, next_date,
                 f"Demo note for {posting_rows[i][2]}." if stage in ("applied", "replied", "interviewing", "offer") else None,
                 pass_reason, "Synthetic pass detail." if pass_reason else None, applied_at, created, updated)
            )
            if applied_at or stage == "ready":
                app_rows.append(
                    (pid, applied_at, "company careers page", f"DEMO-{pid:06d}" if applied_at else None,
                     created, updated)
                )
                if applied_at:
                    ap = datetime.fromisoformat(applied_at)
                    open_events.append((pid, _iso(ap - timedelta(minutes=rng.randint(3, 14)))))
    conn.executemany(
        "INSERT INTO funnel (posting_id, stage, tier, next_action, next_action_date, notes, pass_reason,"
        " pass_detail, applied_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        funnel_rows,
    )
    conn.executemany(
        "INSERT INTO funnel_events (posting_id, from_stage, to_stage, note, at) VALUES (?,?,?,?,?)",
        event_rows,
    )
    variant_id = conn.execute(
        "INSERT INTO resume_variants (label, path, purpose, reviewed_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("Generalist SWE (demo)", "resume/demo_resume.pdf", "Synthetic demo variant",
         _iso(now - timedelta(days=20)), _iso(now - timedelta(days=30)), _iso(now - timedelta(days=20))),
    ).lastrowid
    conn.executemany(
        "INSERT INTO applications (posting_id, submitted_at, channel, resume_variant_id, confirmation_ref,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        [(a[0], a[1], a[2], variant_id, a[3], a[4], a[5]) for a in app_rows],
    )
    conn.executemany(
        "INSERT INTO activity_events (entity_type, entity_id, action, data, at)"
        " VALUES ('posting', ?, 'apply_lite_open', '{}', ?)",
        open_events,
    )

    # --- drafts (canned text, no model) -------------------------------------------------
    draft_rows = []
    for stage in ("tailored", "ready", "applied", "replied", "interviewing", "offer"):
        for pid in staged.get(stage, [])[:25]:
            i = idx_of[pid]
            for kind in ("tailoring", "cover"):
                draft_rows.append(
                    (pid, kind,
                     f"[demo {kind} draft] Why I fit the {posting_rows[i][3]} role at {posting_rows[i][2]}: "
                     "synthetic placeholder text, no model was called.",
                     "demo-fixture", 0, 0, 0.0, _iso(now - timedelta(days=rng.randint(1, 12))))
                )
    conn.executemany(
        "INSERT INTO drafts (posting_id, kind, content, model, tokens_in, tokens_out, cost_usd, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        draft_rows,
    )

    # --- interviews, offer, contacts, touches, sprint -----------------------------------
    for n, pid in enumerate(staged.get("interviewing", [])):
        when = now + timedelta(days=n - 1, hours=rng.randint(1, 8))  # one past, rest upcoming
        past = n == 0
        conn.execute(
            "INSERT INTO interviews (posting_id, round_name, scheduled_at, interviewers, prep_notes, outcome,"
            " thank_you_due_at, thank_you_completed_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, _ROUNDS[n % len(_ROUNDS)], _iso(when),
             f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)} (demo)",
             "Review the product, prepare two stories, ask about team rituals.",
             "passed" if past else "", _iso(now) if past else None, None,
             _iso(now - timedelta(days=3)), _iso(now - timedelta(days=1))),
        )
    for pid in staged.get("offer", []):
        conn.execute(
            "INSERT INTO offers (posting_id, received_at, deadline, base_salary, bonus, equity, location, notes,"
            " decision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, _iso(now - timedelta(days=2)), (now + timedelta(days=6)).date().isoformat(),
             155000.0, 10000.0, "0.05% over 4 years (synthetic)", "Remote - US",
             "Synthetic offer for the demo.", "", _iso(now - timedelta(days=2)), _iso(now - timedelta(days=2))),
        )

    sprint_pids = staged.get("suggested", [])[:5]
    sprint_companies = [posting_rows[idx_of[p]][2] for p in sprint_pids]
    contact_ids = []
    for n, company in enumerate(sprint_companies[:3] + [c["name"] for c in companies[:3]]):
        cur = conn.execute(
            "INSERT INTO contacts (name, company, title, relationship, source, notes, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"{_FIRST_NAMES[n % len(_FIRST_NAMES)]} {_LAST_NAMES[n % len(_LAST_NAMES)]}", company,
             "Engineering Manager (demo)", "former teammate", "demo", "Synthetic contact.",
             _iso(now - timedelta(days=20)), _iso(now - timedelta(days=20))),
        )
        contact_ids.append(cur.lastrowid)
    for n, cid in enumerate(contact_ids):
        done = n % 2 == 1
        conn.execute(
            "INSERT INTO touches (contact_id, posting_id, kind, status, due_at, completed_at, notes,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, sprint_pids[n] if n < len(sprint_pids) else None, "warm intro request",
             "completed" if done else "planned", _iso(now - timedelta(hours=n)),
             _iso(now - timedelta(days=n)) if done else None, "demo", _iso(now - timedelta(days=5)),
             _iso(now - timedelta(days=1))),
        )
    sprint_id = conn.execute(
        "INSERT INTO sprints (name, status, target_size, started_at, notes) VALUES (?,?,?,?,?)",
        ("Demo Sprint", "active", 5, _iso(now - timedelta(days=2)), "Synthetic sprint."),
    ).lastrowid
    for n, pid in enumerate(sprint_pids):
        conn.execute(
            "INSERT INTO sprint_members (sprint_id, posting_id, added_at, no_warm_path_note) VALUES (?,?,?,?)",
            (sprint_id, pid, _iso(now - timedelta(days=2)), "no contact yet (demo)" if n >= 3 else None),
        )

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "companies": count("SELECT COUNT(*) FROM companies"),
        "postings": count("SELECT COUNT(*) FROM postings"),
        "funnel": {r[0]: r[1] for r in conn.execute("SELECT stage, COUNT(*) FROM funnel GROUP BY stage")},
        "interviews": count("SELECT COUNT(*) FROM interviews"),
    }
