import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SprintPanel } from "./App";
import type { Sprint } from "./types";

afterEach(cleanup);

const sprint: Sprint = {
  id: 1,
  name: "Sprint 6",
  started_at: "2026-07-28",
  target_size: 5,
  members: [
    {
      posting_id: 101,
      company: "Acme Corp",
      title: "Backend Engineer",
      stage: "tailored",
      next_action: "tailor",
      blockers: ["missing referral"],
      no_warm_path_note: null,
    },
    {
      posting_id: 102,
      company: "Globex",
      title: "Platform Engineer",
      stage: "closed",
      next_action: "record outcome",
      blockers: [],
      no_warm_path_note: null,
    },
  ],
};

describe("SprintPanel", () => {
  it("renders member company, title, and next action", () => {
    render(<SprintPanel sprint={sprint} open={() => {}}/>);
    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByText("Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Next: tailor")).toBeInTheDocument();
  });

  it("renders blocker chip text", () => {
    render(<SprintPanel sprint={sprint} open={() => {}}/>);
    expect(screen.getByText("missing referral")).toBeInTheDocument();
  });

  it("calls open with the posting_id when a row is clicked", () => {
    const open = vi.fn();
    render(<SprintPanel sprint={sprint} open={open}/>);
    fireEvent.click(screen.getByText("Acme Corp"));
    expect(open).toHaveBeenCalledWith(101);
  });

  it("shows done for a closed-stage member", () => {
    render(<SprintPanel sprint={sprint} open={() => {}}/>);
    expect(screen.getByText("Next: done")).toBeInTheDocument();
  });
});
