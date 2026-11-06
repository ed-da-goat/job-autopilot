import { describe, expect, it } from "vitest";
import { blockerPassReason } from "./pass-reason";

describe("blockerPassReason mapping", () => {
  const cases: Array<{ name: string; blockers: string[]; expected: string }> = [
    { name: "years-of-experience phrasing", blockers: ["Requires 8+ years experience"], expected: "seniority" },
    { name: "principal title", blockers: ["Principal Engineer role"], expected: "seniority" },
    { name: "visa/sponsorship keyword", blockers: ["No visa sponsorship available"], expected: "sponsorship" },
    { name: "on-site/relocation keyword", blockers: ["On-site only, no relocation"], expected: "geography" },
    { name: "no keyword match falls back", blockers: ["Requires Rust experience"], expected: "not_interested" },
  ];

  it.each(cases)("maps $name to $expected", ({ blockers, expected }) => {
    expect(blockerPassReason(blockers).reason).toBe(expected);
  });
});

describe("blockerPassReason detail", () => {
  it("joins multiple blockers with '; '", () => {
    expect(blockerPassReason(["needs sponsorship", "on-site required"]).detail).toBe(
      "needs sponsorship; on-site required",
    );
  });

  it("truncates detail to 200 characters", () => {
    const longBlocker = "reason ".repeat(50);
    const { detail } = blockerPassReason([longBlocker]);
    expect(detail.length).toBe(200);
    expect(detail).toBe(longBlocker.slice(0, 200));
  });
});
