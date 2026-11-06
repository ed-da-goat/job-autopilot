import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReadyList } from "./App";

afterEach(cleanup);

const row = { posting_id: 1, company: "Acme Corp", title: "Senior Backend Engineer" };

describe("ReadyList", () => {
  it("renders row company and title", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    render(<ReadyList rows={[row]} refresh={vi.fn()} open={vi.fn()}/>);
    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByText("Senior Backend Engineer")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("posts confirm-applied and refreshes when Submitted is clicked", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    const refresh = vi.fn().mockResolvedValue(undefined);

    render(<ReadyList rows={[row]} refresh={refresh} open={vi.fn()}/>);
    fireEvent.click(screen.getByText("Submitted"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/posting/1/confirm-applied",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ confirmed: true, confirmation_ref: "" }),
      }),
    );
    await waitFor(() => expect(refresh).toHaveBeenCalled());

    vi.unstubAllGlobals();
  });

  it("calls open with the posting_id when the row body is clicked", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    const open = vi.fn();
    render(<ReadyList rows={[row]} refresh={vi.fn()} open={open}/>);
    fireEvent.click(screen.getByText("Acme Corp"));
    expect(open).toHaveBeenCalledWith(1);
    vi.unstubAllGlobals();
  });

  it("renders the error detail inline when the request fails", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 422, json: async () => ({ detail: "already confirmed" }) });
    vi.stubGlobal("fetch", fetchMock);

    render(<ReadyList rows={[row]} refresh={vi.fn()} open={vi.fn()}/>);
    fireEvent.click(screen.getByText("Submitted"));

    await waitFor(() => expect(screen.getByText("already confirmed")).toBeInTheDocument());

    vi.unstubAllGlobals();
  });
});
