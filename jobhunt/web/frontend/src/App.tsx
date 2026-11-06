import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, BriefcaseBusiness, CalendarDays, Check, ChevronRight, CircleAlert,
  Command, HeartHandshake, LayoutDashboard, Search, Users, X,
} from "lucide-react";
import { api, post } from "./api";
import { keyboardIntent, shouldIgnoreShortcut, type TriageAction } from "./keyboard";
import { blockerPassReason } from "./pass-reason";
import type { ApplySession, ConferenceAction, GhostCheckItem, Invariants, PostingDetail, Role, Sprint, SprintMember, Today } from "./types";

type View = "Today" | "Pipeline" | "People" | "Interviews" | "Analytics";
const views: Array<[View, typeof LayoutDashboard]> = [
  ["Today", LayoutDashboard], ["Pipeline", BriefcaseBusiness], ["People", Users],
  ["Interviews", CalendarDays], ["Analytics", Activity],
];

function jsonList(value?: string): string[] {
  if (!value) return [];
  try { return JSON.parse(value); } catch { return []; }
}

function jsonObject(value?: string): Record<string, string> {
  if (!value) return {};
  try { return JSON.parse(value); } catch { return {}; }
}

function relative(value?: string) {
  if (!value) return "never";
  const days = Math.floor((Date.now() - new Date(value).getTime()) / 86_400_000);
  return days <= 0 ? "today" : `${days}d ago`;
}

export function App() {
  const [view, setView] = useState<View>("Today");
  const [today, setToday] = useState<Today | null>(null);
  const [selected, setSelected] = useState(0);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [notice, setNotice] = useState("");

  const refreshToday = useCallback(() => api<Today>("/api/today").then(setToday), []);
  useEffect(() => { refreshToday().catch((error) => setNotice(error.message)); }, [refreshToday]);

  const triage = useCallback(async (action: TriageAction) => {
    if (!today?.eligible.length) return;
    const role = today.eligible[Math.min(selected, today.eligible.length - 1)];
    const snapshot = today;
    setToday({ ...today, eligible: today.eligible.filter((item) => item.posting_id !== role.posting_id) });
    setSelected((value) => Math.max(0, Math.min(value, today.eligible.length - 2)));
    try {
      const body = action === "pass"
        ? { decision: "pass", reason: "not_interested" }
        : { decision: "qualify", tier: action };
      await post(`/api/posting/${role.posting_id}/decision`, body);
      setNotice(`${role.company} moved out of triage`);
    } catch (error) {
      setToday(snapshot);
      setNotice((error as Error).message);
    }
  }, [selected, today]);

  // Today's own triage keyboard flow only ever operates on data.eligible
  // (qualify/pass decisions). Outcome capture reuses the same one-keystroke
  // pattern against data.ghost_check — the oldest no-reply application — so
  // it stays wired to Today rather than the Pipeline view's card list.
  const recordOutcome = useCallback(async (postingId: number, outcome: "replied" | "screen" | "ghosted") => {
    if (!today) return;
    const snapshot = today;
    setToday({ ...today, ghost_check: today.ghost_check.filter((item) => item.posting_id !== postingId) });
    try {
      await post(`/api/posting/${postingId}/outcome`, { outcome });
      setNotice(`Recorded ${outcome} for posting ${postingId}`);
    } catch (error) {
      setToday(snapshot);
      setNotice((error as Error).message);
    }
  }, [today]);

  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (shouldIgnoreShortcut(event.target)) return;
      const intent = keyboardIntent(event);
      if (!intent) return;
      if (intent === "search") { event.preventDefault(); setSearchOpen(true); return; }
      if (intent === "escape") { setSearchOpen(false); setDetailId(null); return; }
      if (view !== "Today") return;
      if (intent === "next") setSelected((i) => Math.min(i + 1, (today?.eligible.length || 1) - 1));
      if (intent === "previous") setSelected((i) => Math.max(0, i - 1));
      if (["fast", "top20", "pass"].includes(intent)) void triage(intent as TriageAction);
      if (intent === "replied" || intent === "ghosted") {
        const oldest = today?.ghost_check[0];
        if (oldest) void recordOutcome(oldest.posting_id, intent);
      }
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [today, triage, recordOutcome, view]);

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span>J</span><div>Job Search OS<small>Local command center</small></div></div>
      <nav>{views.map(([name, Icon]) => <button key={name} className={view === name ? "active" : ""} onClick={() => setView(name)}><Icon size={17}/>{name}</button>)}</nav>
      <button className="command-button" onClick={() => setSearchOpen(true)}><Search size={16}/> Search <kbd>⌘K</kbd></button>
      <a className="legacy" href="/legacy">Legacy workspace</a>
      <div className="privacy">Local only<br/><span>No automatic sends or submissions</span></div>
    </aside>
    <main>
      {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice("")}><X size={14}/></button></div>}
      {view === "Today" && <TodayView data={today} selected={selected} setSelected={setSelected} open={setDetailId} triage={triage} refresh={refreshToday} outcome={recordOutcome}/>}
      {view === "Pipeline" && <PipelineView open={setDetailId}/>} 
      {view === "People" && <PeopleView/>}
      {view === "Interviews" && <InterviewsView/>}
      {view === "Analytics" && <AnalyticsView/>}
    </main>
    {detailId !== null && <DetailPanel postingId={detailId} close={() => setDetailId(null)} changed={refreshToday}/>} 
    {searchOpen && <SearchPalette close={() => setSearchOpen(false)} choose={(result) => { setSearchOpen(false); if (result.type === "posting") setDetailId(result.id); else setView("People"); }}/>} 
  </div>;
}

function Header({ eyebrow, title, subtitle }: { eyebrow: string; title: string; subtitle: string }) {
  return <header className="page-header"><div><p>{eyebrow}</p><h1>{title}</h1><span>{subtitle}</span></div><div className="keys"><kbd>J</kbd><kbd>K</kbd> move <kbd>F</kbd> fast <kbd>T</kbd> top <kbd>X</kbd> pass</div></header>;
}

function TodayView({ data, selected, setSelected, open, triage, refresh, outcome }: { data: Today | null; selected: number; setSelected: (n: number) => void; open: (id: number) => void; triage: (a: TriageAction) => void; refresh: () => Promise<void>; outcome: (postingId: number, outcome: "replied" | "screen" | "ghosted") => void }) {
  if (!data) return <Loading/>;
  const current = data.eligible[selected];
  const staleSources = data.health.sources.filter(source => source.stale);
  return <>
    <Header eyebrow="Daily operating view" title="Today" subtitle="Clear the eligible queue, then work the promises you already made."/>
    {data.sprint && <SprintPanel sprint={data.sprint} open={open}/>}
    <section className="metrics">
      <Metric label="Eligible triage" value={data.eligible.length} tone="gold"/>
      <Metric label="Needs judgment" value={data.review.length} tone="blue"/>
      <Metric label="Ready to submit" value={data.ready.length} tone="green"/>
      <Metric label="Blocked in funnel" value={data.blocked_funnel.length} tone="red"/>
    </section>
    <ApplySessionControl/>
    {(staleSources.length > 0 || !data.health.scheduler_installed) && <div className="health-warning"><CircleAlert size={18}/><div><strong>Source health</strong><span>{staleSources.length ? `Stale: ${staleSources.map(source => source.source).join(", ")}` : "Sources are fresh"} · Daily automation {data.health.scheduler_installed ? "installed" : "not installed (opt-in)"}</span></div><small>{data.health.sources.length ? `Latest ${relative(data.health.sources.map(source => source.last_success_at).filter(Boolean).sort().at(-1))}` : "No sweeps"}</small></div>}
    <div className="today-grid">
      <section className="panel triage-panel">
        <div className="panel-head"><div><span className="kicker">PRIMARY QUEUE</span><h2>Eligible roles</h2></div><span>{data.eligible.length} remaining</span></div>
        {!current ? <Empty icon={<Check/>} title="Eligible queue clear" body="No obviously blocked roles are waiting."/> : <>
          <div className="triage-card" onClick={() => open(current.posting_id)}>
            <div className="score">{current.total ?? "–"}<small>match</small></div>
            <div><span className="company">{current.company}</span><h3>{current.title}</h3><p>{current.location || "Location not listed"} · {current.variants?.length || 1} source link{(current.variants?.length || 1) > 1 ? "s" : ""}</p></div><ChevronRight/>
          </div>
          <div className="evidence">{jsonList(current.evidence).slice(0, 3).map((item) => <span key={item}>{item}</span>)}</div>
          <div className="triage-actions"><button onClick={() => triage("pass")} className="danger">Pass <kbd>X</kbd></button><button onClick={() => triage("fast")}>Fast lane <kbd>F</kbd></button><button onClick={() => triage("top20")} className="primary">Top 20 <kbd>T</kbd></button></div>
          <div className="queue-strip">{data.eligible.slice(0, 12).map((role, index) => <button aria-label={`Select ${role.company}`} className={index === selected ? "selected" : ""} key={role.posting_id} onClick={() => setSelected(index)}>{role.company.slice(0, 1)}</button>)}</div>
        </>}
      </section>
      <section className="panel">
        <div className="panel-head"><div><span className="kicker">REQUIRES JUDGMENT</span><h2>Review queue</h2></div><span>{data.review.length}</span></div>
        <div className="compact-list">{data.review.slice(0, 8).map((role) => <button key={role.posting_id} onClick={() => open(role.posting_id)}><div><strong>{role.company}</strong><span>{role.title}</span><small>{role.location || "Unknown geography"}</small></div><b>{role.total ?? "–"}</b></button>)}</div>
      </section>
    </div>
    <section className="work-grid">
      <InvariantPanel inv={data.invariants} open={open}/>
      <BlockedList rows={data.blocked_funnel} refresh={refresh} open={open}/>
      <ReadyList rows={data.ready} refresh={refresh} open={open}/>
      <WorkList title="Touches due" rows={data.touches} empty="No follow-ups due"/>
      <WorkList title="Interview work" rows={data.interviews} empty="No prep or thank-yous due"/>
      <WorkList title="Offer deadlines" rows={data.offers} empty="No active offer deadline"/>
      <GhostCheckList rows={data.ghost_check} outcome={outcome}/>
      <ConferenceList rows={data.conference_actions} refresh={refresh}/>
    </section>
  </>;
}

export function ApplySessionControl() {
  const [session, setSession] = useState<ApplySession | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(() => api<ApplySession>("/api/apply-session").then(setSession), []);
  useEffect(() => { refresh().catch((e) => setError((e as Error).message)); }, [refresh]);
  useEffect(() => {
    if (!session?.running) return;
    const timer = setInterval(() => { void refresh().catch((e) => setError((e as Error).message)); }, 5000);
    return () => clearInterval(timer);
  }, [session?.running, refresh]);

  const start = async () => {
    setError("");
    try {
      setSession(await post<ApplySession>("/api/apply-session", { limit: 10 }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!session) return null;
  const lastLine = session.log_tail.at(-1);
  return <div className="apply-session-control">
    {session.running
      ? <button disabled>Session running — review the pre-filled tabs and submit each by hand</button>
      : <>
          {session.exit_code !== null && <span>Last session exited (code {session.exit_code})</span>}
          <button onClick={() => void start()}>Start apply session</button>
        </>}
    {lastLine && <small>{lastLine}</small>}
    {error && <span className="error-text">{error}</span>}
  </div>;
}

const SPRINT_DONE_STAGES = new Set(["passed", "closed"]);

export function SprintPanel({ sprint, open }: { sprint: Sprint; open: (id: number) => void }) {
  const count = sprint.members.length;
  return <section className="panel sprint-panel">
    <div className="panel-head"><div><span className="kicker">ACTIVE SPRINT</span><h2>{sprint.name}</h2></div><span>{count}/{sprint.target_size ?? count} roles</span></div>
    <div className="sprint-rows">{sprint.members.map((member: SprintMember) => {
      const done = SPRINT_DONE_STAGES.has(member.stage);
      return <div key={member.posting_id} className={done ? "sprint-row done" : "sprint-row"} onClick={() => open(member.posting_id)}>
        <div><strong>{member.company}</strong><span>{member.title}</span></div>
        <span className="stage-badge">{member.stage}</span>
        <span className="next-action">{`Next: ${done ? "done" : member.next_action}`}</span>
        <div className="row-actions">{member.blockers.map((blocker) => <span className="sprint-blocker" key={blocker}>{blocker}</span>)}</div>
      </div>;
    })}</div>
  </section>;
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) { return <div className={`metric ${tone}`}><span>{label}</span><strong>{value}</strong></div>; }
function Empty({ icon, title, body }: { icon: React.ReactNode; title: string; body: string }) { return <div className="empty">{icon}<strong>{title}</strong><span>{body}</span></div>; }
function WorkList({ title, rows, empty }: { title: string; rows: Record<string, any>[]; empty: string }) { return <section className="panel mini"><h3>{title}<span>{rows.length}</span></h3>{rows.length ? rows.slice(0, 4).map((row, i) => <div className="work-row" key={row.id || row.posting_id || i}><strong>{row.company || row.contact_name || row.posting_company || row.round_name || "Action"}</strong><span>{row.title || row.kind || row.deadline || row.due_at || "Open"}</span>{row.posting_status && row.posting_status !== "active" && <em>Source closed</em>}</div>) : <p>{empty}</p>}</section>; }
export function BlockedList({ rows, refresh, open }: { rows: Role[]; refresh: () => Promise<void>; open: (id: number) => void }) {
  const remove = async (row: Role) => {
    const { reason, detail } = blockerPassReason(jsonList(row.blockers));
    await post(`/api/posting/${row.posting_id}/decision`, { decision: "pass", reason, detail });
    await refresh();
  };
  return <section className="panel mini"><h3>Needs removal<span>{rows.length}</span></h3>{rows.length ? rows.slice(0, 4).map((row) => <div className="work-row" key={row.posting_id}><div className="row-body" onClick={() => open(row.posting_id)}><strong>{row.company}</strong><span>{row.title}</span></div><div className="row-actions"><button className="retire" onClick={() => void remove(row)}>Remove</button></div></div>) : <p>No blocked roles remain in the active funnel</p>}</section>;
}
export function ReadyList({ rows, refresh, open }: { rows: Record<string, any>[]; refresh: () => Promise<void>; open: (id: number) => void }) {
  const [errors, setErrors] = useState<Record<number, string>>({});
  const submit = async (postingId: number) => {
    setErrors((prev) => { const next = { ...prev }; delete next[postingId]; return next; });
    try {
      await post(`/api/posting/${postingId}/confirm-applied`, { confirmed: true, confirmation_ref: "" });
      await refresh();
    } catch (e) {
      setErrors((prev) => ({ ...prev, [postingId]: (e as Error).message }));
    }
  };
  return <section className="panel mini"><h3>Ready to submit<span>{rows.length}</span></h3>{rows.length ? rows.slice(0, 4).map((row, i) => <div className="work-row" key={row.posting_id ?? i}><div className="row-body" onClick={() => open(row.posting_id)}><strong>{row.company}</strong><span>{row.title}</span></div><div className="row-actions"><button onClick={() => void submit(row.posting_id)}>Submitted</button></div>{errors[row.posting_id] && <span className="error-text">{errors[row.posting_id]}</span>}</div>) : <p>Nothing waiting for manual submission</p>}</section>;
}
export function InvariantPanel({ inv, open }: { inv: Invariants; open: (id: number) => void }) {
  const total = inv.no_next_action_count + inv.applied_missing_submitted_count;
  if (total === 0) return null;
  return <section className="panel alert">
    <div className="panel-head"><div><span className="kicker">INVARIANT CHECK</span><h2>Dropped balls</h2></div><span>{total}</span></div>
    <div className="invariant-group"><h3>No next action<span>{inv.no_next_action_count}</span></h3>{inv.no_next_action.length ? inv.no_next_action.map((row) => <div className="work-row" key={row.posting_id} onClick={() => open(row.posting_id)}><strong>{row.company} — {row.title}</strong><span>{row.stage}</span></div>) : <p>None</p>}</div>
    <div className="invariant-group"><h3>Applied w/o submit date<span>{inv.applied_missing_submitted_count}</span></h3>{inv.applied_missing_submitted.length ? inv.applied_missing_submitted.map((row) => <div className="work-row" key={row.posting_id} onClick={() => open(row.posting_id)}><strong>{row.company} — {row.title}</strong></div>) : <p>None</p>}</div>
  </section>;
}
function GhostCheckList({ rows, outcome }: { rows: GhostCheckItem[]; outcome: (postingId: number, outcome: "replied" | "screen" | "ghosted") => void }) {
  return <section className="panel mini"><h3>Ghost check<span>{rows.length}</span></h3>{rows.length ? rows.slice(0, 4).map((row, i) => <div className="work-row" key={row.posting_id}><strong>{row.company}</strong><span>{row.title} · applied {relative(row.submitted_at)}</span><div className="row-actions"><button onClick={() => outcome(row.posting_id, "replied")}>Replied{i === 0 ? <kbd>R</kbd> : null}</button><button className="retire" onClick={() => outcome(row.posting_id, "ghosted")}>Ghosted{i === 0 ? <kbd>G</kbd> : null}</button></div></div>) : <p>No applications 14+ days without reply</p>}</section>;
}
function ConferenceList({ rows, refresh }: { rows: ConferenceAction[]; refresh: () => Promise<void> }) {
  const complete = async (id: number, final: boolean) => { if (final && !window.confirm("Retire this action for good? It will never resurface.")) return; await post(`/api/conference_actions/${id}/complete`, { final }); await refresh(); };
  return <section className="panel mini"><h3>Conference funding<span>{rows.length}</span></h3>{rows.length ? rows.slice(0, 4).map((row) => <div className="work-row" key={row.id}><strong>{row.program} — {row.action}</strong><span>{row.notes || row.due_at || "Open"}</span><div className="row-actions"><button title={row.recur_days ? `Done for now — resurfaces in ${row.recur_days}d` : "Complete"} onClick={() => complete(row.id, false)}>Done</button>{row.recur_days ? <button className="retire" title="Retire permanently" onClick={() => complete(row.id, true)}>Retire</button> : null}</div></div>) : <p>No conference actions due</p>}</section>;
}
function Loading() { return <div className="loading">Loading local workspace…</div>; }

const OUTCOME_STAGES = new Set(["applied", "replied", "interviewing"]);

function PipelineView({ open }: { open: (id: number) => void }) {
  const [board, setBoard] = useState<any>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api<any>("/api/board").then(setBoard), []);
  useEffect(() => { load().catch((e) => setError((e as Error).message)); }, [load]);
  if (!board) return <Loading/>;
  const stages = ["qualified", "tailored", "ready", "applied", "replied", "interviewing", "offer"];
  const recordOutcome = async (postingId: number, outcome: "screen" | "rejected") => {
    setError("");
    try {
      await post(`/api/posting/${postingId}/outcome`, { outcome });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return <>
    <Header eyebrow="Full lifecycle" title="Pipeline" subtitle="Every role from qualification through offer, with closed work visibly marked."/>
    {error && <div className="notice" role="status">{error}<button onClick={() => setError("")}><X size={14}/></button></div>}
    <div className="kanban">{stages.map((stage) => <section className="column" key={stage}><h2>{stage}<span>{board.columns[stage]?.length || 0}</span></h2>{(board.columns[stage] || []).map((role: any) => (
      <div
        className="pipeline-card"
        role="button"
        tabIndex={0}
        onClick={() => open(role.posting_id)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(role.posting_id); } }}
        key={role.posting_id}
      >
        <b>{role.company}</b>
        <span>{role.title}</span>
        {role.posting_status !== "active" && <em>Source closed</em>}
        <small>{role.total ?? "–"} match · {role.days_in_stage ?? 0}d</small>
        {OUTCOME_STAGES.has(stage) && <div className="row-actions">
          <button onClick={(e) => { e.stopPropagation(); void recordOutcome(role.posting_id, "screen"); }}>Screen</button>
          <button className="retire" onClick={(e) => { e.stopPropagation(); void recordOutcome(role.posting_id, "rejected"); }}>Rejected</button>
        </div>}
      </div>
    ))}</section>)}</div>
  </>;
}

function PeopleView() {
  const [contacts, setContacts] = useState<any[]>([]); const [touches, setTouches] = useState<any[]>([]);
  const reload = () => Promise.all([api<any[]>("/api/contacts"), api<any[]>("/api/touches")]).then(([c,t]) => { setContacts(c); setTouches(t); });
  useEffect(() => { void reload(); }, []);
  const add = async (event: React.FormEvent<HTMLFormElement>) => { event.preventDefault(); const f = new FormData(event.currentTarget); await post("/api/contacts", Object.fromEntries(f)); event.currentTarget.reset(); await reload(); };
  return <><Header eyebrow="Relationships" title="People" subtitle="Plan and count warm touches only when the touch is actually completed."/><div className="two-col"><section className="panel"><h2>Contacts</h2><form className="inline-form" onSubmit={add}><input required name="name" placeholder="Name"/><input name="company" placeholder="Company"/><input name="relationship" placeholder="Relationship"/><button>Add contact</button></form><div className="table-list">{contacts.map(c => <div key={c.id}><strong>{c.name}</strong><span>{c.company || "No company"}</span><small>{c.relationship}</small></div>)}</div></section><section className="panel"><h2>Touches</h2><div className="table-list">{touches.map(t => <div key={t.id}><strong>{t.kind.replaceAll("_", " ")}</strong><span>{t.status}</span>{t.status !== "completed" && <button onClick={async () => { await post(`/api/touches/${t.id}/complete`, {}); await reload(); }}>Complete</button>}</div>)}</div><a href="/people" className="text-link">Plan a touch in the legacy form</a></section></div></>;
}

function InterviewsView() {
  const [interviews, setInterviews] = useState<any[]>([]); const [offers, setOffers] = useState<any[]>([]);
  useEffect(() => { Promise.all([api<any[]>("/api/interviews"), api<any[]>("/api/offers")]).then(([i,o]) => { setInterviews(i); setOffers(o); }); }, []);
  return <><Header eyebrow="High-intent work" title="Interviews & offers" subtitle="Prepare the next conversation, close the thank-you loop, and keep deadlines visible."/><div className="two-col"><section className="panel"><h2>Interviews</h2><div className="table-list">{interviews.map(i => <div key={i.id}><strong>{i.round_name}</strong><span>Posting #{i.posting_id}</span><small>{i.scheduled_at || "Unscheduled"}</small></div>)}</div></section><section className="panel"><h2>Offers</h2><div className="table-list">{offers.map(o => <div key={o.id}><strong>Posting #{o.posting_id}</strong><span>{o.location || "Location open"}</span><small>{o.deadline || "No deadline"}</small></div>)}</div></section></div><a href="/interviews" className="text-link">Add or update interview and offer details</a></>;
}

function AnalyticsView() {
  const [report, setReport] = useState<any>(null); useEffect(() => { api("/api/report").then(setReport); }, []);
  if (!report) return <Loading/>;
  return <><Header eyebrow="Weekly truth" title="Analytics" subtitle="Applications and warm touches come from explicit completed records, never proxy fields."/><section className="metrics report"><Metric label="Applications · 7d" value={report.applications_7d} tone="green"/><Metric label="Warm touches · 7d" value={report.warm_touches_7d} tone="blue"/><Metric label="Discovered · 7d" value={report.new_postings_7d} tone="gold"/></section><div className="two-col"><section className="panel"><h2>Stage counts</h2>{Object.entries(report.stage_counts).map(([key,value]) => <div className="bar-row" key={key}><span>{key}</span><div><i style={{ width: `${Math.min(100, Number(value) * 8)}%` }}/></div><b>{String(value)}</b></div>)}</section><section className="panel"><h2>Queue precision</h2>{report.queue_precision.length ? report.queue_precision.map((row: any) => <div className="precision" key={row.score_band}><strong>{row.score_band}s</strong><span>{row.accepted} accepted · {row.passed} passed</span></div>) : <Empty icon={<Activity/>} title="No reviewed bands yet" body="Precision appears after triage decisions."/>}</section></div></>;
}

function DetailPanel({ postingId, close, changed }: { postingId: number; close: () => void; changed: () => Promise<void> }) {
  const [detail, setDetail] = useState<PostingDetail | null>(null); const [error, setError] = useState(""); const [artifact, setArtifact] = useState("");
  const reload = useCallback(() => api<PostingDetail>(`/api/posting/${postingId}`).then(setDetail), [postingId]);
  useEffect(() => { reload().catch(e => setError(e.message)); }, [reload]);
  const stage = async (next: string) => { setError(""); try { await post(`/api/posting/${postingId}/stage`, { stage: next }); await Promise.all([reload(), changed()]); } catch (e) { setError((e as Error).message); } };
  const saveApplication = async (event: React.FormEvent<HTMLFormElement>) => { event.preventDefault(); const f = new FormData(event.currentTarget); const resume = f.get("resume_variant_id"); await post(`/api/posting/${postingId}/application`, { resume_variant_id: resume ? Number(resume) : null, channel: f.get("channel"), confirmation_ref: "", notes: f.get("notes") }); await reload(); };
  if (!detail) return <div className="drawer"><button className="close" onClick={close}><X/></button><Loading/></div>;
  const blockers = jsonList(detail.eligibility?.blockers); const warnings = jsonList(detail.eligibility?.warnings); const evidence = jsonList(detail.eligibility?.evidence); const scoreEvidence = jsonObject(detail.score?.score_evidence);
  return <div className="scrim" onMouseDown={e => { if (e.target === e.currentTarget) close(); }}><aside className="drawer"><button className="close" onClick={close}><X/></button><div className="drawer-top"><span className={`eligibility ${detail.eligibility?.status}`}>{detail.eligibility?.status || "unassessed"}</span><strong>{detail.score?.total ?? "–"} match</strong></div><p className="company">{detail.posting.company}</p><h1>{detail.posting.title}</h1><p>{detail.posting.location || "Location not listed"} · {detail.posting_age_days ?? "?"} days old</p>{detail.posting.status !== "active" && <div className="health-warning"><CircleAlert/>This source role is closed.</div>}
    {blockers.map(x => <div className="blocker" key={x}>Blocked: {x}</div>)}{warnings.map(x => <div className="warning" key={x}>Review: {x}</div>)}
    <section><h2>Matched evidence</h2>{Object.entries(scoreEvidence).map(([component, note]) => <p className="quote" key={component}><strong>{component.replaceAll("_", " ")}:</strong> {note}</p>)}{evidence.length ? evidence.map(x => <p className="quote" key={x}>{x}</p>) : Object.keys(scoreEvidence).length === 0 && <p>No evidence excerpt captured.</p>}</section>
    <section><h2>Application workspace</h2><div className={detail.readiness.ready ? "ready-state yes" : "ready-state"}>{detail.readiness.ready ? <Check/> : <CircleAlert/>}{detail.readiness.ready ? "Ready gate passed" : detail.readiness.blockers.join(" · ")}</div><form className="application-form" onSubmit={saveApplication}><label>Reviewed resume<select name="resume_variant_id" defaultValue={detail.application?.resume_variant_id || ""}><option value="">Select…</option>{detail.resume_variants.map(r => <option key={r.id} value={r.id} disabled={!r.reviewed_at}>{r.label}{r.reviewed_at ? "" : " · unreviewed"}</option>)}</select></label><label>Channel<input name="channel" defaultValue={detail.application?.channel || "company careers page"}/></label><label>Notes<textarea name="notes" defaultValue={detail.application?.notes || ""}/></label><button>Save workspace</button></form><div className="stage-actions"><button onClick={() => stage("tailored")}>Tailored</button><button onClick={() => stage("ready")}>Check & mark ready</button><button className="primary" onClick={async () => { if (!window.confirm("Confirm you submitted this application externally?")) return; try { await post(`/api/posting/${postingId}/confirm-applied`, { confirmed: true }); await Promise.all([reload(), changed()]); } catch(e) { setError((e as Error).message); } }}>Mark applied</button></div>{error && <div className="blocker">{error}</div>}</section>
    <section><h2>Copy-ready artifacts</h2><div className="artifact-actions">{["follow_up", "interview_prep", "thank_you", "offer_questions"].map(kind => <button key={kind} onClick={async () => { const row = await post<any>(`/api/posting/${postingId}/artifacts`, { kind }); setArtifact(row.content); await reload(); }}>{kind.replaceAll("_", " ")}</button>)}</div>{artifact && <textarea className="artifact" readOnly value={artifact}/>}</section>
    <section><h2>Cleaned description</h2><div className="description">{detail.description || "No description captured."}</div></section>
    <section><h2>Source variants</h2>{detail.duplicate_group.map((v:any) => <a target="_blank" rel="noreferrer" href={v.url} key={v.id}>{v.source} · {v.location || "location open"}</a>)}</section>
    <a className="text-link" href={`/posting/${postingId}`}>Open full legacy detail</a>
  </aside></div>;
}

function SearchPalette({ close, choose }: { close: () => void; choose: (result: any) => void }) {
  const [query, setQuery] = useState(""); const [rows, setRows] = useState<any[]>([]);
  useEffect(() => { let current = true; const timer = setTimeout(() => api<any[]>(`/api/search?q=${encodeURIComponent(query)}`).then(data => { if (current) setRows(data); }), 100); return () => { current = false; clearTimeout(timer); }; }, [query]);
  return <div className="palette-scrim" onMouseDown={e => { if (e.currentTarget === e.target) close(); }}><div className="palette" role="dialog" aria-label="Search jobs and actions"><div><Command/><input autoFocus value={query} onChange={e => setQuery(e.target.value)} placeholder="Search jobs, companies, or actions…"/><kbd>Esc</kbd></div>{rows.map(row => <button key={`${row.type}-${row.id}`} onClick={() => choose(row)}><Search size={16}/><span><strong>{row.primary}</strong>{row.secondary}</span><b>{row.meta ?? "–"}</b></button>)}</div></div>;
}
