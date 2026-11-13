// Werkstatt → Lösungen (ADR-0035): the explorer over installed solutions in
// declaration format 2 - a tree by scope (root, packages) and type folder,
// an element panel (BPMN with bpmn-js, DMN with dmn-js, briefings and tools
// as their TOML with a parsed view), and "Prüfen": the proving run with
// the result coloured into the diagram. Editing writes back through the
// API, which validates the whole solution before the runtime reloads.
import { Alert, Anchor, Badge, Button, Card, Code, FileButton, Group, Loader, Menu, Select, Stack, Table, Tabs, Text, Textarea, Title } from "@mantine/core";
import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { type BriefingFields, type ContractInput, type DesignerCatalog, type DraftMeta, type DraftView, type ElementKind, type ModelRow, type GraphNode, type NewCaseBody, type NewElementBody, type NodeKind, type ProcessGraph, type ProvingReport, type RunReport, type SolutionElement, type SolutionRow, type SolutionTree, type TemplateRow } from "../api/client";
import { NewCardDialog } from "../components/CardEditor";
import { DesignerPalette, type PaletteItem } from "../components/DesignerPalette";
import { EdgeSettings, StepSettings } from "../components/StepSettings";
import { TestRunDialog } from "../components/TestRunDialog";
import type { CanvasApi, CanvasLabels } from "../components/FlowCanvas";
import { KINDS, KIND_STYLE } from "../components/canvasKinds";
import { BriefingForm, StepPanel, stepBadges } from "../components/StepPanel";
import { NewCaseDialog, NewElementDialog, NewSolutionDialog, StartDialog } from "../components/WorkshopDialogs";
import { useHousehold } from "../household";
import { useI18n } from "../i18n";
import { TrustBadge } from "./Apps";

const DmnCanvas = lazy(() => import("../components/Diagrams").then((m) => ({ default: m.DmnCanvas })));
const FlowCanvas = lazy(() => import("../components/FlowCanvas").then((m) => ({ default: m.FlowCanvas })));

const FOLDERS: { key: keyof Omit<SolutionTree, "where" | "packages" | "samples">; kind: ElementKind | null }[] = [
  { key: "processes", kind: "process" },
  { key: "decisions", kind: "decision" },
  { key: "ai", kind: "ai" },
  { key: "skills", kind: "skill" },
  { key: "tools", kind: "tool" },
  { key: "types", kind: null },
  { key: "knowledge", kind: null },
  { key: "doctypes", kind: null },
];

interface Selection {
  app: string;
  kind: ElementKind;
  id: string;
}

function TreeNode({ tree, app, depth, selected, onSelect }: { tree: SolutionTree; app: string; depth: number; selected: Selection | null; onSelect: (s: Selection) => void }) {
  const { t } = useI18n();
  return (
    <Stack gap={2} pl={depth * 12}>
      {FOLDERS.map(({ key, kind }) => {
        const items = tree[key];
        if (!items.length) return null;
        return (
          <div key={key}>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase">{t(`solutions.folder.${key}`)}</Text>
            {items.map((id) =>
              kind ? (
                <Anchor key={id} component="button" type="button" size="sm" display="block" fw={selected?.app === app && selected.kind === kind && selected.id === id ? 700 : 400} onClick={() => onSelect({ app, kind, id })}>
                  {id}
                </Anchor>
              ) : (
                <Text key={id} size="sm" c="dimmed">{id}</Text>
              ),
            )}
          </div>
        );
      })}
      {Object.entries(tree.packages).map(([name, child]) => (
        <div key={name}>
          <Text size="xs" fw={700} tt="uppercase">{t("solutions.folder.package")} {name}</Text>
          <TreeNode tree={child} app={app} depth={depth + 1} selected={selected} onSelect={onSelect} />
        </div>
      ))}
    </Stack>
  );
}

export function SolutionsPage() {
  const { t, pick, lang } = useI18n();
  const { members, selected: member } = useHousehold();
  const candidates = members.filter((m) => m.info);
  const state = candidates.find((m) => m.member.id === (member !== "all" ? member : candidates.find((c) => c.member.type !== "mailroom")?.member.id ?? candidates[0]?.member.id));
  const [solutions, setSolutions] = useState<SolutionRow[]>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [element, setElement] = useState<SolutionElement | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [editing, setEditing] = useState(false);
  const [report, setReport] = useState<ProvingReport | null>(null);
  const [configuration, setConfiguration] = useState("test");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [focusBriefing, setFocusBriefing] = useState<SolutionElement | null>(null);
  const [newSolution, setNewSolution] = useState(false);
  const [newElement, setNewElement] = useState<{ app: string; kind: "process" | "ai" | "decision" } | null>(null);
  const [newCase, setNewCase] = useState(false);
  const [starting, setStarting] = useState(false);
  // The card canvas (ADR-0040): the graph from the core, the edited copy, what is picked.
  const [graph, setGraph] = useState<ProcessGraph | null>(null);
  const [graphDraft, setGraphDraft] = useState<ProcessGraph | null>(null);
  const [contractDraft, setContractDraft] = useState<ContractInput[] | null>(null);
  const [catalog, setCatalog] = useState<DesignerCatalog | null>(null);
  const [testRun, setTestRun] = useState(false);
  const [runReport, setRunReport] = useState<RunReport | null>(null);
  const [edgeFocus, setEdgeFocus] = useState<string | null>(null);
  const [newCard, setNewCard] = useState<NodeKind | null>(null);
  const [templates, setTemplates] = useState<TemplateRow[]>([]);
  const canvas = useRef<CanvasApi | null>(null);
  const [wide, setWide] = useState(false); // explorer folded away: room for the canvas
  const [fullscreen, setFullscreen] = useState(false); // the canvas fills the window, panel as overlay
  const [tab, setTab] = useState<string | null>(null);

  const [registry, setRegistry] = useState<ModelRow[]>([]);
  // ADR-0039 stage 1: imports and template copies wait as drafts
  const [drafts, setDrafts] = useState<DraftMeta[]>([]);
  const [draftView, setDraftView] = useState<DraftView | null>(null);
  const load = () => {
    if (!state) return;
    state.client.solutions().then(setSolutions).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
    state.client.models().then(setRegistry).catch(() => setRegistry([]));
  };
  useEffect(load, [state]); // eslint-disable-line react-hooks/exhaustive-deps
  // Deep link (ADR-0063): ?solution=<id>&kind=process&id=<element>&node=<step>
  // opens exactly that element - the operator's admin links here to edit.
  const [params] = useSearchParams();
  const linked = useRef(false);
  const linkedNode = useRef<string | null>(null);
  useEffect(() => {
    if (linked.current || !solutions.length) return;
    const app = params.get("solution");
    if (!app) return;
    linked.current = true;
    const row = solutions.find((x) => x.id === app);
    if (!row) return;
    const kind = (params.get("kind") ?? "process") as ElementKind;
    const id = params.get("id") ?? (kind === "process" ? row.tree.processes[0] : undefined);
    linkedNode.current = params.get("node");
    if (id) setSelection({ app, kind, id });
  }, [solutions, params]);
  const draftSolution = (selection?.app ?? solutions[0]?.id) || "";
  const draftCount = solutions.find((s) => s.id === draftSolution)?.drafts ?? 0;
  useEffect(() => {
    if (!state || !draftSolution || !draftCount) { setDrafts([]); return; }
    state.client.solutionDrafts(draftSolution).then(setDrafts).catch(() => setDrafts([]));
  }, [state, draftSolution, draftCount]);
  const openDraft = (id: string) => {
    if (!state) return;
    state.client.draft(id, lang).then(setDraftView).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  };
  const draftAction = async (kind: "check" | "release" | "discard") => {
    if (!state || !draftView) return;
    setBusy(true);
    setError(null);
    try {
      if (kind === "check") {
        await state.client.checkDraft(draftView.id);
        openDraft(draftView.id);
      } else if (kind === "release") {
        const out = await state.client.releaseDraft(draftView.id);
        setNotice(t("solutions.drafts.released", { title: draftView.title }));
        const proc = out.files.find((f) => f.startsWith("processes/") && f.endsWith(".bpmn"));
        setDraftView(null);
        load();
        if (proc) setSelection({ app: out.solution, kind: "process", id: proc.slice("processes/".length, -".bpmn".length) });
      } else {
        await state.client.discardDraft(draftView.id);
        setNotice(t("solutions.drafts.discarded", { title: draftView.title }));
        setDraftView(null);
        load();
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    if (!state || !selection) return;
    setElement(null);
    setEditing(false);
    setFocus(null);
    setGraph(null);
    setGraphDraft(null);
    setContractDraft(null);
    setRunReport(null);
    setEdgeFocus(null);
    setTab(null);
    setFullscreen(false);
    state.client.solutionElement(selection.app, selection.kind, selection.id).then((el) => { setElement(el); setDraft(el.text); }).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
    if (selection.kind === "process") {
      state.client.processGraph(selection.app, selection.id).then((g) => { setGraph(g); const want = linkedNode.current; linkedNode.current = null; if (want && g.nodes.some((n) => n.id === want)) setFocus(want); }).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
    }
  }, [state, selection]);
  useEffect(() => {
    if (!state) return;
    state.client.templates().then(setTemplates).catch(() => undefined);
  }, [state]);
  // The designer's building blocks for the solution being edited (ADR-0062).
  useEffect(() => {
    if (!state || !selection || selection.kind !== "process" || !editing) return;
    let live = true;
    state.client.designerCatalog(selection.app, lang).then((c) => { if (live) setCatalog(c); }).catch(() => undefined);
    return () => { live = false; };
  }, [state, selection, editing, lang]);
  useEffect(() => {
    if (!editing) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); canvas.current?.undo(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing]);
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFullscreen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);
  // A picked step or edge opens the properties tab.
  useEffect(() => { if (focus || edgeFocus) setTab("properties"); }, [focus, edgeFocus]);

  const solution = solutions.find((s) => s.id === selection?.app) ?? solutions[0];
  // every installed process can be the template of a new one (ADR-0035)
  const processTemplates = useMemo(() => solutions.flatMap((s) => s.tree.processes.map((p) => ({ value: `${s.id}/${p}`, label: `${p} · ${s.id}` }))), [solutions]);
  const focusedStep = element?.summary.steps?.find((s) => s.id === focus);

  // The briefing behind the focused AI step, for the property panel.
  useEffect(() => {
    setFocusBriefing(null);
    if (!state || !selection || !focusedStep?.briefing) return;
    let live = true;
    state.client.solutionElement(selection.app, "ai", focusedStep.briefing).then((el) => { if (live) setFocusBriefing(el); }).catch(() => undefined);
    return () => { live = false; };
  }, [state, selection, focusedStep?.briefing]); // eslint-disable-line react-hooks/exhaustive-deps

  const badgeLabels = useMemo(() => ({
    classify: t("solutions.panel.classify"), assign: t("solutions.panel.assign"), unbound: t("solutions.unbound"),
    internal: t("solutions.panel.internal"), read: t("solutions.panel.read"), external: t("solutions.panel.external"), mandate: t("solutions.panel.mandate"),
    categorization: t("solutions.panel.categorization"), deadline: t("solutions.panel.deadline"),
    waitsFor: t("solutions.panel.waitsForShort"), each: t("solutions.panel.each"), client: t("solutions.panel.client"),
  }), [t]);
  const badges = useMemo(() => {
    if (!element || element.kind !== "process") return null;
    return Object.fromEntries((element.summary.steps ?? []).map((s) => [s.id, stepBadges(s, solution, badgeLabels)]));
  }, [element, solution, badgeLabels]);

  const canvasLabels = useMemo<CanvasLabels>(() => ({
    kind: Object.fromEntries(KINDS.map((k) => [k, t(`solutions.card.kind.${k}`)])) as CanvasLabels["kind"],
    outcome: t("solutions.card.outcome"),
    deleteEdge: t("solutions.card.deleteEdge"),
    otherwise: t("solutions.card.otherwise"),
    or: t("solutions.card.or"),
    fields: { knowledge_hits: t("solutions.card.field.knowledge_hits"), verdict: t("solutions.card.field.verdict"), confidence: t("solutions.card.field.confidence") },
  }), [t]);
  const subtitles = useMemo(() => {
    if (!element || element.kind !== "process") return {};
    const out: Record<string, string> = {};
    for (const s of element.summary.steps ?? []) {
      if (s.briefing) out[s.id] = `${s.briefing}${s.model_role ? ` · ${s.model_role}` : ""}`;
      else if (s.decision) out[s.id] = `DMN ${s.decision}`;
      else if (s.process) out[s.id] = `→ ${s.process}`;
      else if (s.tool) out[s.id] = s.tool;
      else if (s.action === "record" && s.collection) out[s.id] = `→ ${s.collection}`;
      else if (s.action) out[s.id] = s.action;
      else if (s.kind === "human_gate" && s.approving_outcomes?.length) out[s.id] = `✓ ${s.approving_outcomes.join(", ")}`;
      else if (s.kind === "trigger" && s.source) out[s.id] = t(`solutions.new.source_${s.source}`);
    }
    for (const n of graph?.nodes ?? []) if (n.kind === "end" && n.ref) out[n.id] = `→ ${n.ref}`;
    return out;
  }, [element, graph, t]);
  const liveGraph = graphDraft ?? graph;
  const focusedNode = liveGraph?.nodes.find((n) => n.id === focus) ?? null;
  const focusedEdge = liveGraph?.edges.find((e) => e.id === edgeFocus) ?? null;
  const placeCard = (node: Omit<GraphNode, "x" | "y" | "w" | "h">) => {
    const nodes = liveGraph?.nodes ?? [];
    const x = nodes.length ? Math.max(...nodes.map((n) => n.x)) + 180 : 160;
    const compact = ["start", "end", "timer"].includes(node.kind);
    const size = compact ? 36 : ["xor", "parallel"].includes(node.kind) ? 50 : 100;
    canvas.current?.addNode({ ...node, x, y: 120, w: size, h: compact ? 36 : size === 50 ? 50 : 80 });
    setNewCard(null);
    setFocus(node.id);
  };

  const contract = contractDraft ?? graph?.contract ?? [];
  const placeFromPalette = (item: PaletteItem) => {
    if (item.create) {
      if (selection) setNewElement({ app: selection.app, kind: item.create });
      return;
    }
    const nodes = liveGraph?.nodes ?? [];
    const taken = new Set(nodes.map((n) => n.id));
    const base = (item.ref ?? (item.kind === "xor" ? "choice" : item.kind === "parallel" ? "split" : item.kind === "end" ? "finish" : item.kind)).toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^[^a-z]+/, "") || "step";
    let id = base.slice(0, 40);
    for (let i = 2; taken.has(id); i += 1) id = `${base.slice(0, 36)}${i}`;
    const anchor = nodes.find((n) => n.id === focus);
    const compact = ["start", "end", "timer"].includes(item.kind);
    const w = compact ? 36 : ["xor", "parallel"].includes(item.kind) ? 50 : 100;
    const h = compact ? 36 : w === 50 ? 50 : 80;
    let x = anchor ? anchor.x + 170 : nodes.length ? Math.max(...nodes.map((n) => n.x)) + 170 : 60;
    let y = anchor ? anchor.y : 120;
    while (nodes.some((n) => Math.abs(n.x - x) < 80 && Math.abs(n.y - y) < 60)) y += 110;
    if (item.kind === "start" && nodes.some((n) => n.kind === "start")) { x = 0; y = Math.max(...nodes.map((n) => n.y)) + 130; }
    canvas.current?.addNode({
      id, kind: item.kind, label: item.label.replace(/^↪ /, "").slice(0, 60), x, y, w, h,
      ...(item.ref ? { ref: item.ref } : {}), ...(item.source ? { source: item.source } : {}),
      ...(item.seconds ? { seconds: item.seconds } : {}), ...(item.config ? { config: item.config } : {}),
    }, anchor && anchor.kind !== "end" ? anchor.id : null);
    setFocus(id);
  };
  const runTest = async (payload: Record<string, unknown>) => {
    if (!state || !selection) throw new Error("no process");
    const out = await state.client.runProcess(selection.id, payload);
    setRunReport(out);
    return out;
  };
  const openElement = (kind: "ai" | "decision" | "tool", id: string) => {
    if (!selection) return;
    if (graphDraft || contractDraft) { setError(t("solutions.designer.saveFirst")); return; }
    setSelection({ app: selection.app, kind, id });
  };

  const highlight = useMemo(() => {
    if (!element || element.kind !== "process") return null;
    const steps = element.summary.steps ?? [];
    if (runReport && !report) {
      const visited = Object.fromEntries(Object.keys(runReport.outputs).map((k) => [k, 1]));
      const failed = Object.entries(runReport.outputs).filter(([, o]) => o.outcome === "failed").map(([k]) => k);
      return { visited, untouched: steps.map((s) => s.id).filter((id) => !(id in visited)), failed };
    }
    if (report) {
      const cov = report.coverage[element.id];
      const visited: Record<string, number> = {};
      for (const r of report.results) if (r.subject_id === element.id) for (const s of r.visited) visited[s] = (visited[s] ?? 0) + 1;
      const failed = report.results.filter((r) => r.subject_id === element.id && !r.passed).flatMap((r) => r.visited.slice(-1));
      return { visited, untouched: steps.map((s) => s.id).filter((id) => !(cov?.steps_visited ?? []).includes(id)), failed };
    }
    const stats = element.summary.stats ?? {};
    return { visited: stats, untouched: [], failed: [] };
  }, [element, report, runReport]);

  const save = async () => {
    if (!state || !selection || !element) return;
    setBusy(true);
    setError(null);
    try {
      // The canvas and the source are two views of one file: a changed
      // source wins (it is the whole truth); otherwise the graph is applied.
      const designed = graphDraft ?? (contractDraft ? graph : null);
      const out = draft !== element.text || !designed
        ? await state.client.saveSolutionElement(selection.app, selection.kind, selection.id, draft)
        : await state.client.saveProcessGraph(selection.app, selection.id, { nodes: designed.nodes, edges: designed.edges, ...(contractDraft ? { contract: contractDraft } : {}) });
      setNotice(out.warnings.length ? t("solutions.savedWithWarnings", { warnings: out.warnings.join("; ") }) : t("solutions.saved"));
      setEditing(false);
      setGraphDraft(null);
      setContractDraft(null);
      const el = await state.client.solutionElement(selection.app, selection.kind, selection.id);
      setElement(el);
      setDraft(el.text);
      if (selection.kind === "process") setGraph(await state.client.processGraph(selection.app, selection.id));
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const prove = async (only?: string) => {
    if (!state || !solution) return;
    setBusy(true);
    setError(null);
    try {
      setReport(await state.client.proveSolution(solution.id, { configuration, only: only ?? null }));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const reloadElement = async () => {
    if (!state || !selection) return;
    const el = await state.client.solutionElement(selection.app, selection.kind, selection.id);
    setElement(el);
    setDraft(el.text);
    if (selection.kind === "process") setGraph(await state.client.processGraph(selection.app, selection.id));
    load();
  };

  const saveBriefingFields = async (id: string, fields: BriefingFields) => {
    if (!state || !selection) return;
    setBusy(true);
    setError(null);
    try {
      const out = await state.client.saveBriefingFields(selection.app, id, fields);
      setNotice(out.warnings.length ? t("solutions.savedWithWarnings", { warnings: out.warnings.join("; ") }) : t("solutions.saved"));
      await reloadElement();
      if (focusedStep?.briefing === id) setFocusBriefing(await state.client.solutionElement(selection.app, "ai", id));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
      throw err;
    } finally {
      setBusy(false);
    }
  };

  const fail = (err: unknown) => setError(err instanceof Error ? err.message : String(err));

  const createSolution = async (body: { id: string; name: string; description: string }) => {
    if (!state) return;
    setBusy(true);
    setError(null);
    try {
      const row = await state.client.createSolution(body);
      setNewSolution(false);
      setNotice(t("solutions.new.createdSolution", { id: row.id, path: row.path }));
      load();
    } catch (err: unknown) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const createElement = async (kind: "process" | "ai" | "decision", body: NewElementBody) => {
    if (!state || !newElement) return;
    setBusy(true);
    setError(null);
    try {
      const [srcSolution, srcProcess] = (body.template ?? "").split("/");
      const copied = kind === "process" && srcSolution && srcProcess
        ? await state.client.copyProcess(newElement.app, { source_solution: srcSolution, source_process: srcProcess, id: body.id })
        : null;
      const out = copied ?? await state.client.createSolutionElement(newElement.app, kind, { ...body, template: undefined });
      const app = newElement.app;
      setNewElement(null);
      load();
      if (copied?.draft) {
        // ADR-0039: a copy from a template waits as a draft until released
        setNotice(t("solutions.drafts.created", { title: copied.draft.title }));
        openDraft(copied.draft.id);
      } else {
        setNotice(t("solutions.new.createdElement", { files: out.files.join(", ") }));
        setSelection({ app, kind, id: out.id });
      }
    } catch (err: unknown) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  // Import: a .bpmn into the chosen solution, a bundle (.tgz) as a solution
  const importFile = async (file: File | null) => {
    if (!state || !file || !solution) return;
    setBusy(true);
    setError(null);
    try {
      if (file.name.endsWith(".tgz") || file.name.endsWith(".tar.gz")) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let bin = "";
        for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        const out = await state.client.installBundleFile(file.name, btoa(bin));
        setNotice(t("solutions.importedBundle", { id: out.id, version: out.version }));
      } else if (file.name.toLowerCase().endsWith(".json") || file.name.toLowerCase().endsWith(".zip")) {
        // ADR-0060: n8n / Activepieces / Make / Zapier (also the Zapier ZIP) - a draft with a report
        let zip = "";
        if (file.name.toLowerCase().endsWith(".zip")) {
          const bytes = new Uint8Array(await file.arrayBuffer());
          let bin = "";
          for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
          zip = btoa(bin);
        }
        const out = await state.client.importForeign(solution.id, zip ? "" : await file.text(), "", zip);
        setNotice(t("solutions.drafts.createdForeign", { title: out.draft.title, ...out.report.counts }));
        openDraft(out.draft.id);
      } else {
        const out = await state.client.importProcess(solution.id, file.name, await file.text());
        if (out.draft) {
          setNotice(t("solutions.drafts.created", { title: out.draft.title }));
          openDraft(out.draft.id);
        } else {
          setNotice(t("solutions.importedProcess", { id: out.id, app: solution.id }));
          setSelection({ app: solution.id, kind: "process", id: out.id });
        }
      }
      load();
    } catch (err: unknown) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const startProcess = async (payload: Record<string, unknown>, file?: { name: string; content_base64: string }) => {
    if (!state || !selection) return;
    setBusy(true);
    setError(null);
    try {
      const out = await state.client.startTemplate(selection.id, payload, file);
      setStarting(false);
      setNotice(t("solutions.start.started", { id: out.case_id.slice(0, 12) }));
      await reloadElement();
    } catch (err: unknown) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const createCase = async (body: NewCaseBody) => {
    if (!state || !selection || selection.kind !== "process") return;
    setBusy(true);
    setError(null);
    try {
      const out = await state.client.createSolutionCase(selection.app, selection.id, body);
      setNewCase(false);
      setNotice(t("solutions.new.createdCase", { id: out.id }));
      if (out.run) setReport(out.run);
      await reloadElement();
    } catch (err: unknown) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const newMenu = (s: SolutionRow) => (
    <Menu shadow="md" position="bottom-end">
      <Menu.Target><Button size="compact-xs" variant="light">{t("solutions.new.menu")}</Button></Menu.Target>
      <Menu.Dropdown>
        <Menu.Item onClick={() => setNewElement({ app: s.id, kind: "process" })}>{t("solutions.new.process")}</Menu.Item>
        <Menu.Item onClick={() => setNewElement({ app: s.id, kind: "ai" })}>{t("solutions.new.ai")}</Menu.Item>
        <Menu.Item onClick={() => setNewElement({ app: s.id, kind: "decision" })}>{t("solutions.new.decision")}</Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("solutions.title")}</Title>
        {state && <Button size="xs" onClick={() => setNewSolution(true)}>{t("solutions.new.solution")}</Button>}
      </Group>
      <NewSolutionDialog opened={newSolution} busy={busy} onClose={() => setNewSolution(false)} onCreate={createSolution} />
      <NewElementDialog kind={newElement?.kind ?? null} solution={solutions.find((s) => s.id === newElement?.app) ?? null} templates={processTemplates} busy={busy} onClose={() => setNewElement(null)} onCreate={createElement} />
      <NewCardDialog kind={newCard} solution={solution} templates={templates} taken={(liveGraph?.nodes ?? []).map((n) => n.id)} onClose={() => setNewCard(null)} onCreate={placeCard} />
      <StartDialog opened={starting} processId={selection?.id ?? ""} contract={(element?.summary.contract?.input ?? {}) as Record<string, string>} initial={Object.fromEntries(Object.entries((element?.summary.contract?.defaults ?? {}) as Record<string, unknown>).map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : String(v)]))} busy={busy} onClose={() => setStarting(false)} onStart={startProcess} />
      <TestRunDialog opened={testRun} processId={selection?.id ?? ""} contract={graph?.contract ?? []} onClose={() => setTestRun(false)} onRun={runTest} />
      <NewCaseDialog
        opened={newCase}
        steps={element?.summary.steps ?? []}
        samples={solution?.tree.samples ?? []}
        busy={busy}
        canRecord={Object.values(solution?.model_roles ?? {}).some((r) => !!r.model)}
        onClose={() => setNewCase(false)}
        onCreate={createCase}
      />
      <Text c="dimmed" size="sm">{t("solutions.intro")}</Text>
      {error && <Alert color="red" withCloseButton onClose={() => setError(null)}>{t("app.error", { message: error })}</Alert>}
      {notice && <Alert color="teal" withCloseButton onClose={() => setNotice(null)}>{notice}</Alert>}
      {!solutions.length && <Text c="dimmed">{t("solutions.none")}</Text>}
      <Group align="flex-start" wrap="nowrap">
        <Stack gap="md" w={wide ? 36 : 280} style={{ flexShrink: 0 }}>
          <Button size="compact-xs" variant="subtle" onClick={() => setWide((w) => !w)} title={t(wide ? "solutions.showExplorer" : "solutions.hideExplorer")}>{wide ? "▶" : "◀"}</Button>
          {!wide && solutions.map((s) => (
            <Card key={s.id} withBorder padding="sm">
              <Group gap={6} mb={4} justify="space-between" wrap="nowrap">
                <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                  <Text fw={700} size="sm" truncate>{pick(s.name) || s.id}</Text>
                  <Badge size="xs" variant="outline">v{s.version}</Badge>
                  {!s.writable && <Badge size="xs" color="gray">{t("solutions.readOnly")}</Badge>}
                </Group>
                {s.writable && newMenu(s)}
              </Group>
              {s.trust && s.trust.level !== "local" && <Group gap={6} mb={4}><TrustBadge level={s.trust.level} verified={s.trust.verified} />{s.trust.signed_by && <Text size="xs" c="dimmed">{s.trust.signed_by}</Text>}</Group>}
              {s.id === "core" && <Text size="xs" c="dimmed" mb={4}>{pick(s.description)}</Text>}
              <TreeNode tree={s.tree} app={s.id} depth={0} selected={selection} onSelect={setSelection} />
              <Text size="xs" c="dimmed" mt={6}>{t("solutions.sidecars", { n: s.sidecars })}</Text>
            </Card>
          ))}
        </Stack>
        <Stack style={{ flex: 1, minWidth: 0 }}>
          {solution && (
            <Card withBorder padding="sm">
              <Group justify="space-between">
                <Group gap="xs">
                  <Title order={5}>{t("solutions.prove")}</Title>
                  <Select size="xs" w={140} data={solution.configurations} value={configuration} onChange={(v) => v && setConfiguration(v)} />
                </Group>
                <Group gap="xs">
                  {solution.writable && state && <Button size="xs" variant="default" component="a" href={state.client.solutionExportUrl(solution.id)} title={t("solutions.exportHint")}>{t("solutions.export")}</Button>}
                  {solution.writable && (
                    <FileButton onChange={(f) => void importFile(f)} accept=".bpmn,.xml,.tgz,.json,.zip,application/gzip,application/json,application/zip">
                      {(props) => <Button size="xs" variant="default" loading={busy} title={t("solutions.importHint")} {...props}>{t("solutions.import")}</Button>}
                    </FileButton>
                  )}
                  <Button size="xs" loading={busy} onClick={() => prove()}>{t("solutions.proveAll")}</Button>
                </Group>
              </Group>
              {drafts.length > 0 && (
                <Group gap={6} mt="xs" align="center">
                  <Text size="xs" fw={600}>{t("solutions.drafts.title")}</Text>
                  {drafts.map((d) => (
                    <Button key={d.id} size="compact-xs" variant={draftView?.id === d.id ? "filled" : "light"} color={d.findings.length ? "orange" : "teal"} onClick={() => openDraft(d.id)}>
                      {d.title}
                    </Button>
                  ))}
                </Group>
              )}
              {Object.keys(solution.model_roles).length > 0 && (
                <Group gap="xs" mt="xs" align="flex-end">
                  {Object.entries(solution.model_roles).map(([name, role]) => (
                    <Select key={name} size="xs" w={240} label={`${name} · ${role.max_data_class}`}
                      title={role.reason ?? role.model_ref ?? ""}
                      error={role.model ? undefined : role.reason ?? t("solutions.unbound")}
                      description={role.model ? `${role.model_ref ?? ""} · ${role.pinned ? t("solutions.modelPinned") : t("solutions.modelAuto")}` : undefined}
                      data={[{ value: "", label: t("solutions.modelAutomatic") }, ...registry.map((m) => ({ value: m.id, label: `${m.id} (${m.model})` }))]}
                      value={role.pinned ?? ""}
                      onChange={(v) => { if (!state) return; state.client.pinModelRole(solution.id, name, v || null).then(load).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err))); }} />
                  ))}
                </Group>
              )}
              {report && (
                <Stack gap={4} mt="sm">
                  <Group gap="xs">
                    <Badge color={report.ok ? "teal" : "red"}>{report.ok ? t("solutions.passed") : t("solutions.failed")}</Badge>
                    <Text size="sm">{t("solutions.reportLine", { passed: report.results.filter((r) => r.passed).length, total: report.results.length, ms: report.duration_ms, configuration: report.configuration })}</Text>
                  </Group>
                  {report.errors.map((e) => <Text key={e} size="sm" c="red">{e}</Text>)}
                  <Table withTableBorder={false} verticalSpacing={2}>
                    <Table.Tbody>
                      {report.results.map((r) => (
                        <Table.Tr key={r.case_id}>
                          <Table.Td w={60}><Badge size="xs" color={r.passed ? "teal" : "red"}>{r.passed ? "ok" : "FAIL"}</Badge></Table.Td>
                          <Table.Td><Code>{r.subject_kind}/{r.subject_id}</Code> {r.case_id}{r.attempts > 1 ? ` (pass^${r.attempts})` : ""}</Table.Td>
                          <Table.Td>{r.failures.map((f) => <Text key={f} size="xs" c="red">{f}</Text>)}</Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                  <Group gap="md">
                    {Object.entries(report.coverage).map(([pid, c]) => (
                      <Text key={pid} size="xs" c="dimmed">{pid}: {t("solutions.coverage", { steps: c.steps_visited.length, total: c.steps_total, outcomes: c.outcomes_visited.length, outcomesTotal: c.outcomes_total })}</Text>
                    ))}
                  </Group>
                  {report.missing_recordings.length > 0 && <Text size="xs" c="orange">{t("solutions.missingRecordings", { n: report.missing_recordings.length })}</Text>}
                </Stack>
              )}
            </Card>
          )}
          {draftView && (
            <Card withBorder padding="sm" style={{ borderColor: "var(--mantine-color-orange-5)" }}>
              <Group justify="space-between" align="flex-start">
                <div>
                  <Group gap="xs">
                    <Badge color="orange" variant="light">{t("solutions.drafts.badge")}</Badge>
                    <Title order={5}>{draftView.title}</Title>
                  </Group>
                  <Text size="xs" c="dimmed" mt={4}>
                    {t(`solutions.drafts.origin.${draftView.origin.startsWith("import") ? "import" : "template"}`)}{draftView.origin.startsWith("import:") ? ` (${draftView.origin.slice(7)})` : ""} · {draftView.created_at.slice(0, 16).replace("T", " ")} · {draftView.solution}
                  </Text>
                </div>
                <Group gap="xs">
                  <Button size="xs" variant="default" loading={busy} onClick={() => void draftAction("check")}>{t("solutions.drafts.check")}</Button>
                  <Button size="xs" loading={busy} disabled={draftView.findings.length > 0} title={draftView.findings.length ? t("solutions.drafts.blocked") : t("solutions.drafts.releaseHint")} onClick={() => void draftAction("release")}>{t("solutions.drafts.release")}</Button>
                  <Button size="xs" variant="subtle" color="red" loading={busy} onClick={() => void draftAction("discard")}>{t("solutions.drafts.discard")}</Button>
                  <Button size="xs" variant="subtle" onClick={() => setDraftView(null)}>✕</Button>
                </Group>
              </Group>
              <Text size="sm" mt="xs">{t("solutions.drafts.explain")}</Text>
              {draftView.findings.length > 0 ? (
                <Alert color="orange" mt="xs" title={t("solutions.drafts.findings", { n: draftView.findings.length })}>
                  <Stack gap={2}>{draftView.findings.map((f, i) => <Text key={i} size="xs" ff="monospace">{f}</Text>)}</Stack>
                </Alert>
              ) : (
                <Text size="xs" c="teal" mt="xs">✓ {t("solutions.drafts.valid")}</Text>
              )}
              {draftView.report && "items" in draftView.report && (
                <Card withBorder mt="xs" p="xs">
                  <Group gap="xs" mb={4}>
                    <Text size="xs" fw={600}>{t("solutions.drafts.report.title")}</Text>
                    <Badge size="xs" color="teal" variant="light">{t("solutions.drafts.report.translated")}: {draftView.report.counts.translated}</Badge>
                    <Badge size="xs" color="yellow" variant="light">{t("solutions.drafts.report.approximated")}: {draftView.report.counts.approximated}</Badge>
                    <Badge size="xs" color="red" variant="light">{t("solutions.drafts.report.placeholder")}: {draftView.report.counts.placeholder}</Badge>
                  </Group>
                  {draftView.report.secrets.length > 0 && <Text size="xs">{t("solutions.drafts.report.secrets")}: <Code>{draftView.report.secrets.join(", ")}</Code></Text>}
                  {draftView.report.model_roles.length > 0 && <Text size="xs">{t("solutions.drafts.report.roles", { roles: draftView.report.model_roles.join(", ") })}</Text>}
                  {draftView.report.inputs.length > 0 && <Text size="xs" c="dimmed">{t("solutions.drafts.report.inputs")}: {draftView.report.inputs.join(", ")}</Text>}
                  <Table fz="xs" mt={4} verticalSpacing={2}>
                    <Table.Tbody>
                      {draftView.report.items.filter((i) => i.status !== "translated" || i.note).map((i, k) => (
                        <Table.Tr key={k}>
                          <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}><Text size="xs" fw={600} c={i.status === "placeholder" ? "red" : i.status === "approximated" ? "yellow.8" : "teal"}>● {t(`solutions.drafts.report.${i.status}`)}</Text></Table.Td>
                          <Table.Td style={{ whiteSpace: "nowrap" }}>{i.node}{i.step && <Text span size="xs" c="dimmed"> → {i.step}</Text>}</Table.Td>
                          <Table.Td c="dimmed">{i.note}</Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </Card>
              )}
              {Object.entries(draftView.graphs).map(([pid, g]) => (
                <div key={pid} style={{ marginTop: 8 }}>
                  <Text size="xs" fw={600}>{pid}</Text>
                  {"error" in g ? (
                    <Text size="xs" c="red">{g.error}</Text>
                  ) : (
                    <Suspense fallback={<Loader size="sm" />}>
                      <FlowCanvas graph={g} editable={false} height={420} labels={canvasLabels} />
                    </Suspense>
                  )}
                </div>
              ))}
              <Text size="xs" c="dimmed" mt="xs">{t("solutions.drafts.files")}: {[...draftView.files, ...draftView.changed.map((f) => `${f} (${t("solutions.drafts.changedFile")})`)].join(", ")}</Text>
            </Card>
          )}
          {selection && !element && <Loader size="sm" />}
          {element && (
            <Card withBorder>
              <Group justify="space-between" mb="xs">
                <Group gap="xs">
                  <Title order={4}>{element.id}</Title>
                  <Badge variant="light">{t(`solutions.kind.${element.kind}`)}</Badge>
                  <Code>{element.file}</Code>
                </Group>
                <Group gap="xs">
                  {element.sidecar && <Button size="xs" variant="light" loading={busy} onClick={() => prove()}>{t("solutions.proveThis")}</Button>}
                  {element.kind === "process" && (element.summary.source === "manual" || element.summary.source === "schedule") && !editing && <Button size="xs" color="teal" variant="light" onClick={() => setStarting(true)}>{t("solutions.start.button")}</Button>}
                  {element.kind === "process" && element.summary.source === "manual" && !editing && <Button size="xs" variant="light" onClick={() => setTestRun(true)}>{t("solutions.designer.testRun")}</Button>}
                  {element.writable && element.kind === "process" && <Button size="xs" variant="default" onClick={() => setNewCase(true)}>{t("solutions.new.case")}</Button>}
                  {editing && element.kind === "process" && <Button size="xs" variant="default" onClick={() => canvas.current?.layout()}>{t("solutions.designer.layout")}</Button>}
                  {editing && element.kind === "process" && <Button size="xs" variant="default" disabled={!graphDraft} onClick={() => canvas.current?.undo()} title="Ctrl+Z">{t("solutions.designer.undo")}</Button>}
                  {element.writable && !editing && <Button size="xs" variant="default" onClick={() => setEditing(true)}>{t("solutions.edit")}</Button>}
                  {editing && element.kind === "process" && (
                    <Menu shadow="md" position="bottom-end">
                      <Menu.Target><Button size="xs" variant="light">{t("solutions.card.addMenu")}</Button></Menu.Target>
                      <Menu.Dropdown>
                        {KINDS.map((k) => (
                          <Menu.Item key={k} leftSection={<span style={{ color: KIND_STYLE[k].color }}>{KIND_STYLE[k].glyph}</span>} onClick={() => setNewCard(k)}>{t(`solutions.card.kind.${k}`)}</Menu.Item>
                        ))}
                      </Menu.Dropdown>
                    </Menu>
                  )}
                  {editing && <Button size="xs" loading={busy} onClick={save}>{t("solutions.save")}</Button>}
                  {editing && <Button size="xs" variant="subtle" onClick={() => { setEditing(false); setDraft(element.text); setGraphDraft(null); setContractDraft(null); }}>{t("solutions.cancel")}</Button>}
                </Group>
              </Group>
              {element.kind === "process" && (
                <div style={fullscreen ? { position: "fixed", inset: 0, zIndex: 300, background: "var(--mantine-color-body)", display: "flex", flexDirection: "column" } : { position: "relative" }}>
                  {fullscreen && (
                    <Group justify="space-between" px="sm" py={6} style={{ borderBottom: "1px solid var(--mantine-color-default-border)" }}>
                      <Group gap="xs">
                        <Title order={5}>{element.id}</Title>
                        {graphDraft && editing && <Badge size="xs" color="orange">{t("solutions.card.unsaved")}</Badge>}
                      </Group>
                      <Group gap="xs">
                        {element.writable && !editing && <Button size="xs" variant="default" onClick={() => setEditing(true)}>{t("solutions.edit")}</Button>}
                        {editing && (
                          <Menu shadow="md" position="bottom-end">
                            <Menu.Target><Button size="xs" variant="light">{t("solutions.card.addMenu")}</Button></Menu.Target>
                            <Menu.Dropdown>
                              {KINDS.map((k) => (
                                <Menu.Item key={k} leftSection={<span style={{ color: KIND_STYLE[k].color }}>{KIND_STYLE[k].glyph}</span>} onClick={() => setNewCard(k)}>{t(`solutions.card.kind.${k}`)}</Menu.Item>
                              ))}
                            </Menu.Dropdown>
                          </Menu>
                        )}
                        {editing && <Button size="xs" variant="default" onClick={() => canvas.current?.layout()}>{t("solutions.designer.layout")}</Button>}
                        {editing && <Button size="xs" variant="default" disabled={!graphDraft} onClick={() => canvas.current?.undo()}>{t("solutions.designer.undo")}</Button>}
                        {editing && <Button size="xs" loading={busy} onClick={save}>{t("solutions.save")}</Button>}
                        {editing && <Button size="xs" variant="subtle" onClick={() => { setEditing(false); setDraft(element.text); setGraphDraft(null); setContractDraft(null); }}>{t("solutions.cancel")}</Button>}
                        <Button size="xs" variant="default" onClick={() => setFullscreen(false)}>{t("solutions.card.exitFullscreen")}</Button>
                      </Group>
                    </Group>
                  )}
                  <div style={{ display: "flex", gap: 8, flex: 1, minHeight: 0, padding: fullscreen ? 8 : 0 }}>
                  {editing && (
                    <div style={{ width: 210, flexShrink: 0 }}>
                      <DesignerPalette catalog={catalog} onPick={placeFromPalette} height={fullscreen ? "100%" : 640} />
                    </div>
                  )}
                  <div style={{ position: "relative", flex: 1, minHeight: 0, minWidth: 0 }}>
                    {graph ? (
                      <Suspense fallback={<Loader size="sm" />}>
                        <FlowCanvas
                          graph={graph}
                          editable={editing}
                          highlight={highlight}
                          badges={badges}
                          subtitles={subtitles}
                          selected={focus}
                          selectedEdge={edgeFocus}
                          height={fullscreen ? "100%" : editing ? 640 : 520}
                          labels={canvasLabels}
                          onChange={setGraphDraft}
                          onSelect={setFocus}
                          onSelectEdge={setEdgeFocus}
                          apiRef={canvas}
                        />
                      </Suspense>
                    ) : (
                      <Loader size="sm" />
                    )}
                    {!fullscreen && (
                      <Group gap={6} style={{ position: "absolute", top: 8, right: 8 }}>
                        {graphDraft && editing && <Badge size="xs" color="orange">{t("solutions.card.unsaved")}</Badge>}
                        <Button size="compact-xs" variant="default" onClick={() => setFullscreen(true)} title={t("solutions.card.fullscreen")}>⛶</Button>
                      </Group>
                    )}
                    {fullscreen && (focus || edgeFocus) && !(editing && (focusedNode || focusedEdge)) && (
                      <Card withBorder padding="sm" shadow="md" style={{ position: "absolute", top: 12, right: 12, width: 360, maxHeight: "calc(100% - 24px)", overflow: "auto" }}>
                        <Group justify="flex-end" mb={4}><Button size="compact-xs" variant="subtle" onClick={() => { setFocus(null); setEdgeFocus(null); }}>✕</Button></Group>
                    {focusedStep ? (
                      <StepPanel
                        step={focusedStep}
                        element={element}
                        briefing={focusBriefing}
                        solution={solution}
                        busy={busy}
                        onOpen={(kind, id) => setSelection({ app: selection!.app, kind, id })}
                        onSaveBriefing={(fields) => saveBriefingFields(focusedStep.briefing!, fields)}
                      />
                    ) : !focusedEdge && !(editing && focusedNode) ? (
                      <Stack gap={4}>
                        <Text fw={700}>{pick(element.summary.title) || element.id}</Text>
                        <Text size="xs" c="dimmed">{t("solutions.panel.pickStep")}</Text>
                        {(element.summary.steps ?? []).map((s) => {
                          const h = element.summary.history?.[s.id];
                          return (
                            <Group key={s.id} gap="xs" wrap="nowrap" justify="space-between">
                              <Anchor component="button" type="button" size="sm" onClick={() => setFocus(s.id)}>{s.id}</Anchor>
                              <Group gap={4} wrap="nowrap">
                                <Badge size="xs" variant="outline" color="gray">{t(`workshop.kind.${s.kind}`)}</Badge>
                                {h?.runs ? <Badge size="xs" variant="light" color={h.last_outcome === "failed" ? "red" : "teal"}>{h.runs}</Badge> : null}
                              </Group>
                            </Group>
                          );
                        })}
                      </Stack>
                    ) : focusedNode && !focusedStep && !editing ? (
                      <Stack gap={4}>
                        <Text fw={700}>{focusedNode.label || focusedNode.id}</Text>
                        <Text size="xs" c="dimmed">{t(`solutions.card.kind.${focusedNode.kind}`)}{focusedNode.ref ? ` → ${focusedNode.ref}` : ""}</Text>
                      </Stack>
                    ) : null}
                      </Card>
                    )}
                  </div>
                  {editing && focusedEdge && !focusedNode && liveGraph && (
                    <Card withBorder padding="sm" style={{ width: 360, flexShrink: 0, height: fullscreen ? "100%" : 640, overflow: "auto" }}>
                      <EdgeSettings
                        edge={focusedEdge}
                        graph={liveGraph}
                        catalog={catalog}
                        contract={contract}
                        onApply={(patch) => canvas.current?.updateEdge(focusedEdge.id, patch)}
                        onDelete={() => { canvas.current?.removeSelection(); setEdgeFocus(null); }}
                      />
                    </Card>
                  )}
                  {editing && focusedNode && liveGraph && (
                    <Card withBorder padding="sm" style={{ width: 360, flexShrink: 0, height: fullscreen ? "100%" : 640, overflow: "auto" }}>
                      <StepSettings
                        node={focusedNode}
                        graph={liveGraph}
                        contract={contract}
                        catalog={catalog}
                        solution={solution}
                        templates={templates}
                        onApply={(patch) => canvas.current?.updateNode(focusedNode.id, patch)}
                        onDelete={() => { canvas.current?.removeSelection(); setFocus(null); }}
                        onContract={setContractDraft}
                        onOpen={openElement}
                        onCreate={(kind) => selection && setNewElement({ app: selection.app, kind })}
                      />
                    </Card>
                  )}
                  </div>
                </div>
              )}
              {element.kind === "decision" && (
                <Suspense fallback={<Loader size="sm" />}>
                  <DmnCanvas xml={element.text} />
                </Suspense>
              )}
              <Tabs value={tab ?? (element.kind === "process" ? "properties" : element.kind === "decision" ? "source" : "fields")} onChange={setTab} mt="sm">
                <Tabs.List>
                  {element.kind === "process" && <Tabs.Tab value="properties">{t("solutions.tab.properties")}</Tabs.Tab>}
                  {(element.kind === "ai" || element.kind === "skill" || element.kind === "tool") && <Tabs.Tab value="fields">{t("solutions.tab.fields")}</Tabs.Tab>}
                  <Tabs.Tab value="source">{t("solutions.tab.source")}</Tabs.Tab>
                  {element.sidecar && <Tabs.Tab value="tests">{t("solutions.tab.tests")}</Tabs.Tab>}
                </Tabs.List>
                {element.kind === "process" && (
                  <Tabs.Panel value="properties" pt="xs">
                    <div style={{ maxWidth: 720 }}>
                    {focusedStep ? (
                      <StepPanel
                        step={focusedStep}
                        element={element}
                        briefing={focusBriefing}
                        solution={solution}
                        busy={busy}
                        onOpen={(kind, id) => setSelection({ app: selection!.app, kind, id })}
                        onSaveBriefing={(fields) => saveBriefingFields(focusedStep.briefing!, fields)}
                      />
                    ) : !focusedEdge && !(editing && focusedNode) ? (
                      <Stack gap={4}>
                        <Text fw={700}>{pick(element.summary.title) || element.id}</Text>
                        <Text size="xs" c="dimmed">{t("solutions.panel.pickStep")}</Text>
                        {(element.summary.steps ?? []).map((s) => {
                          const h = element.summary.history?.[s.id];
                          return (
                            <Group key={s.id} gap="xs" wrap="nowrap" justify="space-between">
                              <Anchor component="button" type="button" size="sm" onClick={() => setFocus(s.id)}>{s.id}</Anchor>
                              <Group gap={4} wrap="nowrap">
                                <Badge size="xs" variant="outline" color="gray">{t(`workshop.kind.${s.kind}`)}</Badge>
                                {h?.runs ? <Badge size="xs" variant="light" color={h.last_outcome === "failed" ? "red" : "teal"}>{h.runs}</Badge> : null}
                              </Group>
                            </Group>
                          );
                        })}
                      </Stack>
                    ) : focusedNode && !focusedStep && !editing ? (
                      <Stack gap={4}>
                        <Text fw={700}>{focusedNode.label || focusedNode.id}</Text>
                        <Text size="xs" c="dimmed">{t(`solutions.card.kind.${focusedNode.kind}`)}{focusedNode.ref ? ` → ${focusedNode.ref}` : ""}</Text>
                      </Stack>
                    ) : null}
                    </div>
                  </Tabs.Panel>
                )}
                <Tabs.Panel value="fields" pt="xs">
                  {element.kind === "tool" ? (
                    <Table withRowBorders={false} verticalSpacing={4}>
                      <Table.Tbody>
                        {Object.entries(element.summary).map(([k, v]) => (
                          <Table.Tr key={k}><Table.Td w={130}><Text size="xs" c="dimmed">{k}</Text></Table.Td><Table.Td><Text size="sm">{typeof v === "object" && v ? pick(v as Record<string, string>) || JSON.stringify(v) : String(v ?? "")}</Text></Table.Td></Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                  ) : (
                    <BriefingForm element={element} solution={solution} busy={busy} onSave={element.writable && element.kind === "ai" ? (fields) => saveBriefingFields(element.id, fields) : undefined} />
                  )}
                </Tabs.Panel>
                <Tabs.Panel value="source" pt="xs">
                  {editing ? (
                    <Textarea autosize minRows={8} maxRows={30} value={draft} onChange={(e) => setDraft(e.currentTarget.value)} styles={{ input: { fontFamily: "monospace", fontSize: 12 } }} />
                  ) : (
                    <Code block style={{ maxHeight: 400, overflow: "auto", fontSize: 12 }}>{element.text}</Code>
                  )}
                </Tabs.Panel>
                <Tabs.Panel value="tests" pt="xs">
                  <Code block style={{ maxHeight: 400, overflow: "auto", fontSize: 12 }}>{element.sidecar}</Code>
                </Tabs.Panel>
              </Tabs>
            </Card>
          )}
        </Stack>
      </Group>
    </Stack>
  );
}
