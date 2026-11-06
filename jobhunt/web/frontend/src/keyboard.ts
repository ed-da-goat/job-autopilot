export type TriageAction = "fast" | "top20" | "pass";
export type OutcomeAction = "replied" | "screen" | "ghosted";

export function keyboardIntent(event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey">) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") return "search";
  if (event.key === "j") return "next";
  if (event.key === "k") return "previous";
  if (event.key === "f") return "fast";
  if (event.key === "t") return "top20";
  if (event.key === "x") return "pass";
  if (event.key === "r") return "replied";
  if (event.key === "s") return "screen";
  if (event.key === "g") return "ghosted";
  if (event.key === "Escape") return "escape";
  return null;
}

export function shouldIgnoreShortcut(target: EventTarget | null) {
  const tag = (target as HTMLElement | null)?.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}
