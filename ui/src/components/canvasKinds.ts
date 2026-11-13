// The card vocabulary of the canvas (ADR-0040 §3): the ten kinds of the
// profile, their colour and glyph. No React Flow import here, so the
// shell can show the editors without loading the canvas chunk.
import type { GraphEdge, NodeKind } from "../api/client";

export const KINDS: NodeKind[] = ["start", "ai", "gate", "action", "decision", "call", "timer", "xor", "parallel", "end"];

export const KIND_STYLE: Record<NodeKind, { color: string; glyph: string }> = {
  start: { color: "#0ca678", glyph: "▶" },
  ai: { color: "#7048e8", glyph: "✦" },
  gate: { color: "#f59f00", glyph: "☺" },
  action: { color: "#1c7ed6", glyph: "⚙" },
  decision: { color: "#0c8599", glyph: "⊞" },
  call: { color: "#4263eb", glyph: "↪" },
  timer: { color: "#868e96", glyph: "◷" },
  xor: { color: "#e8590c", glyph: "✕" },
  parallel: { color: "#1c7ed6", glyph: "+" },
  end: { color: "#343a40", glyph: "■" },
};

/** Outcomes the canvas draws as an error path rather than a branch. */
export const ERROR_OUTCOMES = new Set(["failed", "error", "on_error"]);

/** A condition of a gateway flow in short form: "Treffer > 0",
 *  "Urteil = ok, corrected". ``fields`` names known data fields. */
export function conditionText(edge: GraphEdge, fields: Record<string, string>, any: string): string {
  const parts = (edge.when?.conditions ?? []).map((c) => {
    const segments = c.input.split(".");
    const last = segments[segments.length - 1] ?? c.input;
    const name = fields[last] ?? last.replace(/_/g, " ");
    const test = c.test.trim();
    const shown = /^[<>]/.test(test) ? test : test.startsWith("[") || test.startsWith("not(") ? `∈ ${test}` : `= ${test.replace(/"/g, "")}`;
    return `${name} ${shown}`;
  });
  return parts.join(edge.when?.match === "any" ? ` ${any} ` : " · ");
}
