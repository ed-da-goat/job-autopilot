// Maps blocker text on a blocked_funnel row to a decision {reason, detail}
// suitable for POST /api/posting/{id}/decision with decision: "pass".
//
// `reason` must stay within the server's PASS_REASONS set
// (jobhunt/funnel.py): seniority, geography, sponsorship, skills, timing,
// duplicate, not_interested. First matching keyword pattern wins.
export function blockerPassReason(blockers: string[]): { reason: string; detail: string } {
  const text = blockers.join(" ").toLowerCase();

  let reason = "not_interested";
  if (/senior|principal|staff|director|lead|\d+\+?\s*(years|yrs)/.test(text)) {
    reason = "seniority";
  } else if (/visa|sponsor|clearance|citizen/.test(text)) {
    reason = "sponsorship";
  } else if (/locat|on-?site|relocat|geograph|hybrid/.test(text)) {
    reason = "geography";
  }

  const detail = blockers.join("; ").slice(0, 200);
  return { reason, detail };
}
