import { describe, expect, it } from "vitest";
import { keyboardIntent, shouldIgnoreShortcut } from "./keyboard";

describe("keyboard triage", () => {
  it("maps navigation and decision keys", () => {
    expect(keyboardIntent({ key: "j", metaKey: false, ctrlKey: false })).toBe("next");
    expect(keyboardIntent({ key: "k", metaKey: false, ctrlKey: false })).toBe("previous");
    expect(keyboardIntent({ key: "f", metaKey: false, ctrlKey: false })).toBe("fast");
    expect(keyboardIntent({ key: "t", metaKey: false, ctrlKey: false })).toBe("top20");
    expect(keyboardIntent({ key: "x", metaKey: false, ctrlKey: false })).toBe("pass");
  });

  it("maps outcome-capture keys", () => {
    expect(keyboardIntent({ key: "r", metaKey: false, ctrlKey: false })).toBe("replied");
    expect(keyboardIntent({ key: "s", metaKey: false, ctrlKey: false })).toBe("screen");
    expect(keyboardIntent({ key: "g", metaKey: false, ctrlKey: false })).toBe("ghosted");
  });

  it("opens search with command or control K", () => {
    expect(keyboardIntent({ key: "k", metaKey: true, ctrlKey: false })).toBe("search");
    expect(keyboardIntent({ key: "k", metaKey: false, ctrlKey: true })).toBe("search");
  });

  it("protects form fields from global shortcuts", () => {
    expect(shouldIgnoreShortcut(document.createElement("input"))).toBe(true);
    expect(shouldIgnoreShortcut(document.createElement("textarea"))).toBe(true);
    expect(shouldIgnoreShortcut(document.createElement("button"))).toBe(false);
  });
});
