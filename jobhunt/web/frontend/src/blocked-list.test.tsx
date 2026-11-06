import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BlockedList } from "./App";
import type { Role } from "./types";

afterEach(cleanup);

const row: Role = {
  posting_id: 1,
  company: "Acme Corp",
  title: "Senior Backend Engineer",
  location: "Remote",
  blockers: JSON.stringify(["Requires 8+ years experience"]),
  evidence: "[]",
};

describe("BlockedList", () => {
  it("removes a blocked role via a pass decision and refreshes", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    const refresh = vi.fn().mockResolvedValue(undefined);

    render(<BlockedList rows={[row]} refresh={refresh} open={vi.fn()}/>);
    fireEvent.click(screen.getByText("Remove"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/posting/1/decision",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decision: "pass", reason: "seniority", detail: "Requires 8+ years experience" }),
      }),
    );
    await waitFor(() => expect(refresh).toHaveBeenCalled());

    vi.unstubAllGlobals();
  });

  it("calls open with the posting_id when the row body is clicked", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    const open = vi.fn();
    render(<BlockedList rows={[row]} refresh={vi.fn()} open={open}/>);
    fireEvent.click(screen.getByText("Acme Corp"));
    expect(open).toHaveBeenCalledWith(1);
    vi.unstubAllGlobals();
  });
});
