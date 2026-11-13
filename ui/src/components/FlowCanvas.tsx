// The card canvas (ADR-0040): a process as cards and outcome edges on
// React Flow. The graph comes from the core (GET …/graph) and goes back
// as a whole (PUT …/graph); the core applies it onto the BPMN file and
// keeps everything the canvas does not edit. Cards show the declared
// facts (chips) and the proving run or live statistics (ring + count).
// Loaded lazily with the workshop, like the BPMN viewer.
import {
  Background,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  getSmoothStepPath,
  useNodesInitialized,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { GraphEdge, GraphNode, NodeKind, ProcessGraph } from "../api/client";
import { ERROR_OUTCOMES, KIND_STYLE, conditionText } from "./canvasKinds";
import type { Highlight, NodeBadge } from "./Diagrams";

// BPMN boxes are 100x80 on a 180px grid; cards are wider, so the canvas
// stretches the diagram coordinates and folds them back on save. The
// BPMN view (bpmn-js) keeps its own compact layout from the same numbers.
const SCALE_X = 1.45;
const SCALE_Y = 1.3;

const DEFAULT_OUTCOMES: Partial<Record<NodeKind, string[]>> = {
  start: ["fired"],
  ai: ["done", "failed"],
  gate: ["approved", "rejected"],
  action: ["done", "failed"],
  decision: ["done"],
  call: ["done", "failed"],
  timer: ["elapsed"],
};

export interface CardData extends Record<string, unknown> {
  kind: NodeKind;
  label: string;
  ref?: string;
  /** Everything else the core sent for the node (settings, loop, deadline,
   *  channel, names per language): carried through untouched unless edited. */
  spec?: Partial<GraphNode>;
  subtitle?: string;
  badges?: NodeBadge[];
  visited?: number;
  failed?: boolean;
  untouched?: boolean;
  chosen?: boolean;
  labels: CanvasLabels;
}

export interface CanvasLabels {
  kind: Record<NodeKind, string>;
  outcome: string;
  deleteEdge: string;
  /** "otherwise" - the default branch of a deciding gateway (ADR-0056). */
  otherwise: string;
  /** joins conditions of an "any" branch: "or". */
  or: string;
  /** readable names of data fields used in conditions. */
  fields: Record<string, string>;
}

type CardNode = Node<CardData, "card">;
interface EdgeData extends Record<string, unknown> {
  outcome: string;
  when?: GraphEdge["when"];
  default?: boolean;
  text?: string;
  error?: boolean;
}
type OutcomeEdge = Edge<EdgeData, "outcome">;

function edgeData(e: GraphEdge, labels: CanvasLabels): EdgeData {
  const text = e.when?.conditions?.length ? conditionText(e, labels.fields, labels.or) : e.default ? labels.otherwise : e.outcome;
  return { outcome: e.outcome, when: e.when, default: e.default, text, error: ERROR_OUTCOMES.has(e.outcome) };
}

const COLORS: Record<NodeBadge["color"], string> = {
  teal: "#0ca678", blue: "#1c7ed6", violet: "#7048e8", orange: "#e8590c", red: "#e03131", gray: "#868e96", yellow: "#f08c00",
};

function Diamond({ data, selected }: { data: CardData; selected: boolean }) {
  // BPMN: exclusive gateway = diamond with ✕, parallel = diamond with +.
  const style = KIND_STYLE[data.kind];
  const ring = data.failed ? "#e03131" : data.visited ? "#0ca678" : undefined;
  const border = selected || data.chosen ? style.color : ring ?? style.color;
  return (
    <div style={{ width: 120, display: "flex", flexDirection: "column", alignItems: "center", opacity: data.untouched ? 0.55 : 1, position: "relative" }}>
      <div style={{ fontSize: 12, fontWeight: 600, textAlign: "center", minHeight: 16, marginBottom: 4, maxWidth: 160, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{data.label}</div>
      <div style={{ position: "relative", width: 44, height: 44 }}>
        <Handle type="target" position={Position.Left} style={{ background: style.color, width: 9, height: 9, left: -6 }} />
        <Handle type="source" position={Position.Right} style={{ background: style.color, width: 9, height: 9, right: -6 }} />
        <RowHandles />
        <div style={{ position: "absolute", inset: 6, transform: "rotate(45deg)", border: `2.5px solid ${border}`, background: `${style.color}14`, borderRadius: 4, boxShadow: selected || data.chosen ? `0 0 0 3px ${style.color}33` : undefined }} />
        <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", color: style.color, fontWeight: 700, fontSize: data.kind === "parallel" ? 22 : 15 }}>{style.glyph}</div>
      </div>
      {data.visited ? (
        <span style={{ position: "absolute", top: 12, right: 18, background: ring, color: "#fff", borderRadius: 999, fontSize: 10, fontWeight: 700, padding: "1px 6px" }}>{data.visited}</span>
      ) : null}
    </div>
  );
}

// Edges into a row below or above leave at the bottom or top (the second
// row of website-chat: the thorough search). Invisible; the left/right
// handles stay the ones to drag from.
const HIDDEN = { opacity: 0, width: 6, height: 6, pointerEvents: "none" as const };
function RowHandles() {
  return (
    <>
      <Handle id="sb" type="source" position={Position.Bottom} isConnectable={false} style={HIDDEN} />
      <Handle id="st" type="source" position={Position.Top} isConnectable={false} style={HIDDEN} />
      <Handle id="tt" type="target" position={Position.Top} isConnectable={false} style={HIDDEN} />
      <Handle id="tb" type="target" position={Position.Bottom} isConnectable={false} style={HIDDEN} />
    </>
  );
}

/** Which sides an edge uses: down or up when the target sits in another
 * row and not clearly to the right (the same rule as the portal view). */
export function edgeSides(a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }, error: boolean): { sourceHandle?: string; targetHandle?: string } {
  const below = b.y > a.y + a.h, above = b.y + b.h < a.y, right = b.x >= a.x + a.w;
  const close = Math.abs(b.x + b.w / 2 - (a.x + a.w / 2)) < a.w;
  if ((below || above) && (!right || error || close)) {
    return below ? { sourceHandle: "sb", targetHandle: "tt" } : { sourceHandle: "st", targetHandle: "tb" };
  }
  return {};
}

// An entry's symbol (ADR-0057): how the process is triggered.
const ENTRY_GLYPH: Record<string, string> = { chat: "💬", messenger: "📱", schedule: "⏰", folder: "📁", mailbox: "✉", chain: "⛓", manual: "▶" };
function entryGlyph(data: CardData): string {
  return ENTRY_GLYPH[String(data.channel || "")] ?? ENTRY_GLYPH[String(data.source || "")] ?? KIND_STYLE.start.glyph;
}

function Card({ id, data, selected }: NodeProps<CardNode>) {
  if (data.kind === "xor" || data.kind === "parallel") return <Diamond data={data} selected={!!selected} />;
  const style = KIND_STYLE[data.kind];
  const compact = data.kind === "start" || data.kind === "end" || data.kind === "timer";
  const ring = data.failed ? "#e03131" : data.visited ? "#0ca678" : undefined;
  const border = selected || data.chosen ? style.color : ring ?? "var(--mantine-color-default-border)";
  return (
    <div
      style={{
        width: compact ? 120 : 200,
        borderRadius: 10,
        border: `2px solid ${border}`,
        boxShadow: selected || data.chosen ? `0 0 0 3px ${style.color}33` : "0 1px 3px rgba(0,0,0,.12)",
        background: "var(--mantine-color-body)",
        opacity: data.untouched ? 0.55 : 1,
        fontFamily: "inherit",
        position: "relative",
      }}
    >
      {data.kind !== "start" && <Handle type="target" position={Position.Left} style={{ background: style.color, width: 10, height: 10 }} />}
      {data.kind !== "end" && <Handle type="source" position={Position.Right} style={{ background: style.color, width: 10, height: 10 }} />}
      <RowHandles />
      {data.visited ? (
        <span style={{ position: "absolute", top: -9, right: -9, background: ring, color: "#fff", borderRadius: 999, fontSize: 10, fontWeight: 700, padding: "1px 6px" }}>{data.visited}</span>
      ) : null}
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: compact ? "6px 10px" : "8px 10px 4px" }}>
        <span style={{ width: 24, height: 24, borderRadius: 7, background: style.color, color: "#fff", display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: 13, flexShrink: 0 }}>{data.kind === "start" ? entryGlyph(data) : style.glyph}</span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.4, color: "var(--mantine-color-dimmed)" }}>{data.labels.kind[data.kind]}{data.kind === "start" && (data.channel || data.source) ? ` · ${String(data.channel || data.source)}` : ""}</div>
          <div style={{ fontSize: 13, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", color: data.label ? "inherit" : "var(--mantine-color-dimmed)" }}>{data.label || (data.ref ? `→ ${data.ref}` : id)}</div>
        </div>
      </div>
      {!compact && (data.subtitle || data.badges?.length) ? (
        <div style={{ padding: "0 10px 8px" }}>
          {data.subtitle && <div style={{ fontSize: 11, color: "var(--mantine-color-dimmed)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{data.subtitle}</div>}
          {data.badges?.length ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 }}>
              {data.badges.map((b, i) => (
                <span key={i} title={b.title} style={{ fontSize: 10, lineHeight: "16px", padding: "0 6px", borderRadius: 999, background: `${COLORS[b.color]}22`, color: COLORS[b.color], fontWeight: 600 }}>{b.text}</span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Outcome({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected, markerEnd, style }: EdgeProps<OutcomeEdge>) {
  const [path, labelX, labelY] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 12 });
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={{ ...style, strokeWidth: selected ? 2.5 : 1.5, stroke: selected ? "#7048e8" : data?.error ? "#e03131" : "#868e96", strokeDasharray: data?.error ? "5 4" : undefined }} />
      {data?.text ? (
        <EdgeLabelRenderer>
          <div style={{ position: "absolute", transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`, fontSize: 11, padding: "1px 7px", borderRadius: 999, background: selected ? "#7048e8" : data.when ? "#fff4e6" : "var(--mantine-color-body)", color: selected ? "#fff" : data.error ? "#c92a2a" : "inherit", border: `1px solid ${data.when ? "#ffc078" : "var(--mantine-color-default-border)"}`, fontWeight: data.when || data.default ? 600 : 400, pointerEvents: "all" }} className="nodrag nopan">
            {data.text}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

/** Fit the view once the cards are measured, and again when the canvas
 *  changes size (explorer folded, window resized) or another process loads. */
function Fit({ token }: { token: unknown }) {
  const { fitView } = useReactFlow();
  const ready = useNodesInitialized();
  useEffect(() => {
    if (!ready) return;
    const id = window.setTimeout(() => fitView({ padding: 0.12, maxZoom: 1.15, duration: 150 }), 30);
    return () => window.clearTimeout(id);
  }, [ready, token, fitView]);
  useEffect(() => {
    const host = document.querySelector(".react-flow");
    if (!host || typeof ResizeObserver === "undefined") return;
    let last = host.getBoundingClientRect().width;
    const observer = new ResizeObserver(() => {
      const width = host.getBoundingClientRect().width;
      if (Math.abs(width - last) > 40) { last = width; fitView({ padding: 0.12, maxZoom: 1.15, duration: 150 }); }
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [fitView]);
  return null;
}

const nodeTypes = { card: Card };
const edgeTypes = { outcome: Outcome };

export interface CanvasApi {
  addNode: (node: GraphNode, after?: string | null) => void;
  removeSelection: () => void;
  updateNode: (id: string, patch: Partial<GraphNode>) => void;
  updateEdge: (id: string, patch: string | { outcome?: string; when?: GraphEdge["when"] | null; default?: boolean }) => void;
  /** Arrange the cards left to right by their distance from the start. */
  layout: () => void;
  undo: () => void;
  canUndo: () => boolean;
}

const LAYOUT_FIELDS = new Set(["id", "kind", "label", "ref", "x", "y", "w", "h", "source", "seconds"]);
function specOf(n: GraphNode): Partial<GraphNode> {
  return Object.fromEntries(Object.entries(n).filter(([k]) => !LAYOUT_FIELDS.has(k))) as Partial<GraphNode>;
}

/** Ranks by longest path from the starts, ignoring flows back; rows in
 *  order of appearance. Diagram coordinates (before SCALE). */
export function autoLayout(nodes: { id: string; kind: NodeKind }[], edges: { source: string; target: string }[]): Record<string, { x: number; y: number }> {
  const out: Record<string, { x: number; y: number }> = {};
  const next: Record<string, string[]> = {};
  for (const e of edges) (next[e.source] ??= []).push(e.target);
  const rank: Record<string, number> = {};
  const starts = nodes.filter((n) => n.kind === "start").map((n) => n.id);
  const visit = (id: string, depth: number, trail: Set<string>) => {
    if (trail.has(id)) return; // a flow back: no new rank
    if ((rank[id] ?? -1) >= depth) return;
    rank[id] = depth;
    trail.add(id);
    for (const t of next[id] ?? []) visit(t, depth + 1, trail);
    trail.delete(id);
  };
  for (const s of starts) visit(s, 0, new Set());
  let free = Math.max(0, ...Object.values(rank)) + 1;
  for (const n of nodes) if (rank[n.id] === undefined) rank[n.id] = free++;
  const rows: Record<number, number> = {};
  for (const n of nodes) {
    const r = rank[n.id] ?? 0;
    const row = rows[r] ?? 0;
    rows[r] = row + 1;
    out[n.id] = { x: 40 + r * 170, y: 60 + row * 130 };
  }
  return out;
}

export function toGraph(nodes: CardNode[], edges: OutcomeEdge[], sizes: Record<string, { w: number; h: number }>): ProcessGraph {
  return {
    process: "",
    nodes: nodes.map((n) => ({
      ...(n.data.spec ?? {}),
      id: n.id, kind: n.data.kind, label: n.data.label, ref: n.data.ref || undefined,
      x: Math.round(n.position.x / SCALE_X), y: Math.round(n.position.y / SCALE_Y),
      w: sizes[n.id]?.w ?? (["start", "end", "timer"].includes(n.data.kind) ? 36 : ["xor", "parallel"].includes(n.data.kind) ? 50 : 100),
      h: sizes[n.id]?.h ?? (["start", "end", "timer"].includes(n.data.kind) ? 36 : ["xor", "parallel"].includes(n.data.kind) ? 50 : 80),
      source: (n.data.source as string | undefined) || undefined,
      seconds: (n.data.seconds as number | undefined) ?? undefined,
    })),
    edges: edges.map((e) => ({
      id: e.id, source: e.source, target: e.target, outcome: e.data?.outcome ?? "",
      ...(e.data?.when ? { when: e.data.when } : {}), ...(e.data?.default ? { default: true } : {}),
    })),
  };
}

export function FlowCanvas({ graph, editable, highlight, badges, subtitles, selected, selectedEdge, height = 520, labels, onChange, onSelect, onSelectEdge, apiRef }: {
  graph: ProcessGraph;
  editable: boolean;
  highlight?: Highlight | null;
  badges?: Record<string, NodeBadge[]> | null;
  subtitles?: Record<string, string>;
  selected?: string | null;
  selectedEdge?: string | null;
  height?: number | string;
  labels: CanvasLabels;
  onChange?: (graph: ProcessGraph) => void;
  onSelect?: (id: string | null) => void;
  onSelectEdge?: (id: string | null) => void;
  /** Imperative hooks for the page's toolbar (add a card, delete the selection, edit a card). */
  apiRef?: React.MutableRefObject<CanvasApi | null>;
}) {
  const sizes = useRef<Record<string, { w: number; h: number }>>({});
  const build = useCallback((g: ProcessGraph): { nodes: CardNode[]; edges: OutcomeEdge[] } => {
    sizes.current = Object.fromEntries(g.nodes.map((n) => [n.id, { w: n.w, h: n.h }]));
    return {
      nodes: g.nodes.map((n) => ({
        id: n.id, type: "card", position: { x: n.x * SCALE_X, y: n.y * SCALE_Y },
        data: { kind: n.kind, label: n.label, ref: n.ref, source: n.source, channel: n.channel, seconds: n.seconds, spec: specOf(n), labels },
      })),
      edges: g.edges.map((e) => {
        const a = g.nodes.find((n) => n.id === e.source), b = g.nodes.find((n) => n.id === e.target);
        const sides = a && b ? edgeSides(a, b, ERROR_OUTCOMES.has(e.outcome)) : {};
        return { id: e.id, source: e.source, target: e.target, ...sides, type: "outcome", data: edgeData(e, labels), markerEnd: "url(#aitmation-arrow)" };
      }),
    };
  }, [labels]);
  const [nodes, setNodes] = useState<CardNode[]>(() => build(graph).nodes);
  const [edges, setEdges] = useState<OutcomeEdge[]>(() => build(graph).edges);
  const counter = useRef(0);
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  nodesRef.current = nodes;
  edgesRef.current = edges;

  // A new graph from outside (saved, reloaded, other process) replaces the state.
  useEffect(() => {
    const next = build(graph);
    setNodes(next.nodes);
    setEdges(next.edges);
  }, [graph, build]);

  // Undo: the state before each change the canvas reports.
  const history = useRef<{ nodes: CardNode[]; edges: OutcomeEdge[] }[]>([]);
  const committed = useRef<{ nodes: CardNode[]; edges: OutcomeEdge[] } | null>(null);
  useEffect(() => { history.current = []; committed.current = null; }, [graph]);
  const emit = useCallback((n: CardNode[], e: OutcomeEdge[]) => {
    const before = committed.current ?? build(graph);
    history.current = [...history.current.slice(-49), before];
    committed.current = { nodes: n, edges: e };
    onChange?.(toGraph(n, e, sizes.current));
  }, [onChange, build, graph]);

  const onNodesChange = useCallback((changes: NodeChange<CardNode>[]) => {
    setNodes((current) => {
      const next = applyNodeChanges(changes, current);
      if (editable && changes.some((c) => (c.type === "position" && c.dragging === false) || c.type === "remove")) {
        setEdges((e) => { emit(next, e); return e; });
      }
      return next;
    });
  }, [editable, emit]);
  const onEdgesChange = useCallback((changes: EdgeChange<OutcomeEdge>[]) => {
    setEdges((current) => {
      const next = applyEdgeChanges(changes, current);
      if (editable && changes.some((c) => c.type === "remove")) setNodes((n) => { emit(n, next); return n; });
      return next;
    });
  }, [editable, emit]);
  // A new flow gets the first unused outcome of its source's kind.
  const linked = useCallback((n: CardNode[], current: OutcomeEdge[], connection: Connection): OutcomeEdge[] => {
    const source = n.find((x) => x.id === connection.source);
    const used = new Set(current.filter((e) => e.source === connection.source).map((e) => e.data?.outcome));
    const target = n.find((x) => x.id === connection.target);
    const candidates = DEFAULT_OUTCOMES[source?.data.kind ?? "ai"] ?? [];
    let outcome = candidates.find((c) => !used.has(c)) ?? "";
    // Into a pass-through gateway the flow stays unnamed, a parallel
    // split's flows too; into a deciding gateway (ADR-0056) it carries
    // the outcome of the step like any other flow.
    const deciding = target?.data.kind === "xor" && current.some((e) => e.source === target.id && (e.data?.when || e.data?.default));
    if ((target?.data.kind === "xor" && !deciding) || target?.data.kind === "parallel" || source?.data.kind === "parallel") outcome = "";
    else if (!outcome) { let i = 1; while (used.has(`out${i}`)) i += 1; outcome = `out${i}`; }
    counter.current += 1;
    return addEdge<OutcomeEdge>({ ...connection, id: `new_${counter.current}`, type: "outcome", data: { outcome, text: outcome, error: ERROR_OUTCOMES.has(outcome) }, markerEnd: "url(#aitmation-arrow)" }, current);
  }, []);
  const onConnect = useCallback((connection: Connection) => {
    setNodes((n) => {
      setEdges((current) => {
        const next = linked(n, current, connection);
        emit(n, next);
        return next;
      });
      return n;
    });
  }, [emit, linked]);

  useEffect(() => {
    if (!apiRef) return;
    apiRef.current = {
      addNode: (node, after) => {
        setNodes((n) => {
          const next: CardNode[] = [...n.map((x) => ({ ...x, selected: false })), { id: node.id, type: "card", position: { x: node.x * SCALE_X, y: node.y * SCALE_Y }, data: { kind: node.kind, label: node.label, ref: node.ref, source: node.source, seconds: node.seconds, spec: specOf(node), labels }, selected: true }];
          sizes.current[node.id] = { w: node.w, h: node.h };
          setEdges((e) => {
            // Linked only when the card before still has a free outcome of
            // its kind (done, failed, approved …) - never an invented one.
            const anchor = n.find((x) => x.id === after);
            const used = new Set(e.filter((x) => x.source === after).map((x) => x.data?.outcome));
            const free = anchor && (["xor", "parallel"].includes(anchor.data.kind) || (DEFAULT_OUTCOMES[anchor.data.kind] ?? []).some((o) => !used.has(o)));
            const linkedEdges = after && free && node.kind !== "start"
              ? linked(next, e, { source: after, target: node.id, sourceHandle: null, targetHandle: null })
              : e;
            emit(next, linkedEdges);
            return linkedEdges;
          });
          return next;
        });
      },
      removeSelection: () => {
        setNodes((n) => {
          const keep = n.filter((x) => !x.selected);
          setEdges((e) => {
            const ids = new Set(keep.map((x) => x.id));
            const kept = e.filter((x) => !x.selected && ids.has(x.source) && ids.has(x.target));
            emit(keep, kept);
            return kept;
          });
          return keep;
        });
      },
      updateNode: (id, patch) => {
        setNodes((n) => {
          const spec = Object.fromEntries(Object.entries(patch).filter(([k]) => !LAYOUT_FIELDS.has(k)));
          const next = n.map((x) => (x.id === id ? { ...x, data: { ...x.data, ...(patch.label !== undefined ? { label: patch.label } : {}), ...(patch.ref !== undefined ? { ref: patch.ref } : {}), ...(patch.source !== undefined ? { source: patch.source } : {}), ...(patch.seconds !== undefined ? { seconds: patch.seconds } : {}), spec: { ...(x.data.spec ?? {}), ...spec } } } : x));
          setEdges((e) => { emit(next, e); return e; });
          return next;
        });
      },
      layout: () => {
        const n = nodesRef.current, e = edgesRef.current;
        const at = autoLayout(n.map((x) => ({ id: x.id, kind: x.data.kind })), e);
        const next = n.map((x) => ({ ...x, position: { x: (at[x.id]?.x ?? 0) * SCALE_X, y: (at[x.id]?.y ?? 0) * SCALE_Y } }));
        setNodes(next);
        emit(next, e);
      },
      undo: () => {
        const previous = history.current.pop();
        if (!previous) return;
        committed.current = previous;
        setNodes(previous.nodes);
        setEdges(previous.edges);
        onChange?.(toGraph(previous.nodes, previous.edges, sizes.current));
      },
      canUndo: () => history.current.length > 0,
      updateEdge: (id, patch) => {
        const change = typeof patch === "string" ? { outcome: patch } : patch;
        setEdges((e) => {
          const next = e.map((x) => {
            if (x.id !== id) return x;
            const outcome = change.outcome ?? x.data?.outcome ?? "";
            const when = change.when === null ? undefined : change.when ?? x.data?.when;
            const isDefault = change.default ?? x.data?.default ?? false;
            const graphEdge: GraphEdge = { id: x.id, source: x.source, target: x.target, outcome, ...(when ? { when } : {}), ...(isDefault ? { default: true } : {}) };
            return { ...x, data: edgeData(graphEdge, labels) };
          });
          setNodes((n) => { emit(n, next); return n; });
          return next;
        });
      },
    };
    return () => { apiRef.current = null; };
  }, [apiRef, emit, labels, linked, onChange]);

  const decorated = useMemo(() => nodes.map((n) => ({
    ...n,
    data: {
      ...n.data,
      subtitle: subtitles?.[n.id],
      badges: badges?.[n.id],
      visited: highlight?.visited[n.id],
      failed: highlight?.failed.includes(n.id) ?? false,
      untouched: highlight?.untouched.includes(n.id) ?? false,
      chosen: selected === n.id,
    },
  })), [nodes, subtitles, badges, highlight, selected]);
  const decoratedEdges = useMemo(() => edges.map((e) => ({ ...e, selected: e.selected || selectedEdge === e.id })), [edges, selectedEdge]);

  return (
    <div style={{ height, borderRadius: 8, overflow: "hidden", border: "1px solid var(--mantine-color-default-border)" }}>
      <svg style={{ position: "absolute", width: 0, height: 0 }}>
        <defs>
          <marker id="aitmation-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#868e96" />
          </marker>
        </defs>
      </svg>
      <ReactFlowProvider>
      <ReactFlow<CardNode, OutcomeEdge>
        nodes={decorated}
        edges={decoratedEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={(_e, node) => { onSelect?.(node.id); onSelectEdge?.(null); }}
        onEdgeClick={(_e, edge) => { onSelectEdge?.(edge.id); onSelect?.(null); }}
        onPaneClick={() => { onSelect?.(null); onSelectEdge?.(null); }}
        nodesDraggable={editable}
        nodesConnectable={editable}
        elementsSelectable
        deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
        minZoom={0.25}
        maxZoom={2}
      >
        <Background gap={18} size={1} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeColor={(n) => KIND_STYLE[(n.data as CardData).kind].color} style={{ height: 80, width: 120 }} />
        <Fit token={graph} />
      </ReactFlow>
      </ReactFlowProvider>
    </div>
  );
}
