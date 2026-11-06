export type Role = {
  posting_id: number;
  company: string;
  title: string;
  location?: string;
  url?: string;
  total?: number;
  evidence?: string;
  warnings?: string;
  stage?: string;
  blockers?: string;
  variants?: Array<{ posting_id: number; location?: string; url?: string }>;
};

export type Health = {
  scheduler_installed: boolean;
  healthy: boolean;
  stale_after_hours: number;
  sources: Array<{
    source: string;
    last_success_at?: string;
    last_error?: string;
    consecutive_failures: number;
    age_hours?: number;
    stale: boolean;
  }>;
};

export type ConferenceAction = {
  id: number;
  program: string;
  action: string;
  due_at?: string;
  recur_days?: number;
  notes?: string;
  status: string;
};

export type GhostCheckItem = {
  posting_id: number;
  company: string;
  title: string;
  url?: string;
  submitted_at?: string;
};

export type SprintMember = {
  posting_id: number;
  company: string;
  title: string;
  stage: string;
  next_action: string;
  blockers: string[];
  no_warm_path_note: string | null;
};

export type Sprint = {
  id: number;
  name: string;
  started_at: string;
  target_size: number | null;
  members: SprintMember[];
};

export type InvariantRow = {
  posting_id: number;
  company: string;
  title: string;
  stage?: string;
};

export type Invariants = {
  no_next_action: InvariantRow[];
  no_next_action_count: number;
  applied_missing_submitted: InvariantRow[];
  applied_missing_submitted_count: number;
};

export type ApplySession = {
  running: boolean;
  pid: number | null;
  started_at: string | null;
  exit_code: number | null;
  log_tail: string[];
};

export type Today = {
  eligible: Role[];
  review: Role[];
  ready: Record<string, unknown>[];
  blocked_funnel: Role[];
  touches: Record<string, unknown>[];
  interviews: Record<string, unknown>[];
  offers: Record<string, unknown>[];
  conference_actions: ConferenceAction[];
  ghost_check: GhostCheckItem[];
  health: Health;
  sprint: Sprint | null;
  invariants: Invariants;
};

export type PostingDetail = {
  posting: Record<string, any>;
  score: Record<string, any> | null;
  funnel: Record<string, any> | null;
  eligibility: Record<string, any> | null;
  description: string;
  posting_age_days?: number;
  duplicate_group: Record<string, any>[];
  readiness: { ready: boolean; blockers: string[] };
  application: Record<string, any> | null;
  resume_variants: Record<string, any>[];
  artifacts: Record<string, any>[];
};
