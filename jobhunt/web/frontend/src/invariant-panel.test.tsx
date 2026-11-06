import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InvariantPanel } from "./App";
import type { Invariants } from "./types";

afterEach(cleanup);

const zero: Invariants = {
  no_next_action: [],
  no_next_action_count: 0,
  applied_missing_submitted: [],
  applied_missing_submitted_count: 0,
};

const nonZero: Invariants = {
  no_next_action: [{ posting_id: 1, company: "Acme Corp", title: "Backend Engineer", stage: "review" }],
  no_next_action_count: 1,
  applied_missing_submitted: [{ posting_id: 2, company: "Globex", title: "Platform Engineer" }],
  applied_missing_submitted_count: 1,
};

describe("InvariantPanel", () => {
  it("renders nothing when both counts are zero", () => {
    const { container } = render(<InvariantPanel inv={zero} open={vi.fn()}/>);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows both sub-lists and the total when counts are above zero", () => {
    render(<InvariantPanel inv={nonZero} open={vi.fn()}/>);
    expect(screen.getByText("Dropped balls")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("Acme Corp — Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Globex — Platform Engineer")).toBeInTheDocument();
  });

  it("calls open with the posting_id when a no-next-action row is clicked", () => {
    const open = vi.fn();
    render(<InvariantPanel inv={nonZero} open={open}/>);
    fireEvent.click(screen.getByText("Acme Corp — Backend Engineer"));
    expect(open).toHaveBeenCalledWith(1);
  });

  it("calls open with the posting_id when an applied-missing-submitted row is clicked", () => {
    const open = vi.fn();
    render(<InvariantPanel inv={nonZero} open={open}/>);
    fireEvent.click(screen.getByText("Globex — Platform Engineer"));
    expect(open).toHaveBeenCalledWith(2);
  });
});
