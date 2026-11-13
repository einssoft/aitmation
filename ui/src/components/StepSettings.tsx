// The designer's settings panel (ADR-0062): one form per kind of card,
// written into the card's ``config`` (the step's extension element in the
// BPMN file), ``loop``, ``deadline`` - and, for the start, the start form
// (the process contract). Values can be typed as text or taken from the
// data the step can see: the start form, the steps before it, constants.
import { ActionIcon, Badge, Button, Divider, Group, Menu, NumberInput, ScrollArea, Select, Stack, Switch, TagsInput, Text, TextInput, Textarea, Tooltip } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";
import type { ConfigValue, ContractInput, DesignerCatalog, GraphEdge, GraphNode, ProcessGraph, SettingSpec, SolutionRow, StepConfig, TemplateRow } from "../api/client";
import { useI18n } from "../i18n";
import { bindingOptions } from "./CardEditor";
import { KIND_STYLE } from "./canvasKinds";

export interface DataPath {
  path: string;
  group: string;
  label: string;
  description?: string;
}

const PATH = /^(steps\.[a-z][a-z0-9_]*(\.[a-zA-Z0-9_]+)+|trigger\.[a-zA-Z0-9_.]+|const:[a-z][a-z0-9_.]*|each(\.[a-zA-Z0-9_]+)*|case\.(id|process))$/;

/** Steps a flow can come from before ``id``, nearest first. */
export function upstream(graph: ProcessGraph, id: string): string[] {
  const preds: Record<string, string[]> = {};
  for (const e of graph.edges) (preds[e.target] ??= []).push(e.source);
  const seen: string[] = [];
  const queue = [...(preds[id] ?? [])];
  while (queue.length) {
    const cur = queue.shift()!;
    if (cur === id || seen.includes(cur)) continue;
    seen.push(cur);
    queue.push(...(preds[cur] ?? []));
  }
  return seen;
}

/** What a step can bind, from the live (unsaved) graph - the same rules
 *  as the core's GET …/paths. */
export function dataPaths(graph: ProcessGraph, id: string, catalog: DesignerCatalog, contract: ContractInput[]): DataPath[] {
  const out: DataPath[] = [];
  const nodes = Object.fromEntries(graph.nodes.map((n) => [n.id, n]));
  const self = nodes[id];
  if (self?.loop?.over) out.push({ path: "each", group: "each", label: "each", description: self.loop.over });
  for (const c of contract) out.push({ path: `trigger.${c.name}`, group: "start", label: c.name, description: c.description });
  const briefings = Object.fromEntries(catalog.briefings.map((b) => [b.id, b]));
  const actions = Object.fromEntries(catalog.actions.map((a) => [a.id, a]));
  const tools = Object.fromEntries(catalog.tools.map((t) => [t.id, t]));
  const decisions = Object.fromEntries(catalog.decisions.map((d) => [d.id, d]));
  for (const sid of upstream(graph, id)) {
    const n = nodes[sid];
    if (!n || ["start", "xor", "parallel", "end"].includes(n.kind)) continue;
    const title = n.label || sid;
    let fields: Record<string, string> = {};
    const ref = n.ref ?? "";
    if (n.kind === "ai" && briefings[ref]) fields = briefings[ref].outputs;
    else if (n.kind === "action" && actions[ref]) fields = actions[ref].outputs;
    else if (n.kind === "action" && tools[ref]) fields = Object.keys(tools[ref].outputs).length ? tools[ref].outputs : { text: "", data: "" };
    else if (n.kind === "decision" && decisions[ref]) fields = Object.fromEntries(decisions[ref].outputs.map((o) => [o, ""]));
    else if (n.kind === "gate") fields = { comment: "", actor: "" };
    for (const [name, description] of Object.entries(fields)) out.push({ path: `steps.${sid}.${name}`, group: title, label: name, description });
    out.push({ path: `steps.${sid}.outcome`, group: title, label: "outcome" });
  }
  for (const c of catalog.constants) out.push({ path: `const:${c}`, group: "const", label: c });
  out.push({ path: "case.id", group: "case", label: "case.id" });
  return out;
}

/** Typed text becomes a binding the core accepts: a path stays a path, a
 *  text with {…} a template, anything else a literal. */
export function normalizeValue(raw: string): string {
  const v = raw.trim() === "" ? "" : raw;
  if (!v || PATH.test(v.trim()) || v.startsWith("literal:") || v.startsWith("template:")) return v.startsWith("literal:") || v.startsWith("template:") ? v : v.trim();
  return /\{[^}]+\}/.test(v) ? `template:${v}` : `literal:${v}`;
}

function describe(value: string): "path" | "text" | "template" | "" {
  if (!value) return "";
  if (value.startsWith("template:")) return "template";
  if (value.startsWith("literal:")) return "text";
  return "path";
}

/** One bindable value: shows the text without its prefix, a menu inserts data. */
export function ValueField({ label, value, required, multiline, paths, onChange }: {
  label: string;
  value: string;
  required?: boolean;
  multiline?: boolean;
  paths: DataPath[];
  onChange: (v: string) => void;
}) {
  const { t } = useI18n();
  const kind = describe(value);
  const shown = kind === "template" ? value.slice(9) : kind === "text" ? value.slice(8) : value;
  const [text, setText] = useState(shown);
  useEffect(() => setText(shown), [shown]);
  const commit = (raw: string) => onChange(raw === "" ? "" : kind === "path" && PATH.test(raw.trim()) ? raw.trim() : normalizeValue(raw));
  const insert = (path: string) => {
    if (!value) { onChange(path); return; }
    if (kind === "path") { onChange(path); return; }
    onChange(`template:${shown}{${path}}`);
  };
  const groups = [...new Set(paths.map((p) => p.group))];
  const menu = (
    <Menu shadow="md" position="bottom-end" withinPortal>
      <Menu.Target>
        <Tooltip label={t("solutions.designer.insertData")}><ActionIcon size="sm" variant="light">{"{ }"}</ActionIcon></Tooltip>
      </Menu.Target>
      <Menu.Dropdown>
        <ScrollArea.Autosize mah={320} type="auto">
          {groups.map((g) => (
            <div key={g}>
              <Menu.Label>{g === "start" ? t("solutions.designer.fromStart") : g === "const" ? t("solutions.designer.constants") : g === "case" ? t("solutions.designer.case") : g === "each" ? t("solutions.designer.each") : g}</Menu.Label>
              {paths.filter((p) => p.group === g).map((p) => (
                <Menu.Item key={p.path} onClick={() => insert(p.path)}>
                  <Text size="xs">{p.label}</Text>
                  {p.description ? <Text size="xs" c="dimmed" lineClamp={1}>{p.description}</Text> : null}
                </Menu.Item>
              ))}
            </div>
          ))}
          {!paths.length && <Menu.Label>{t("solutions.designer.noData")}</Menu.Label>}
        </ScrollArea.Autosize>
      </Menu.Dropdown>
    </Menu>
  );
  const badge = kind ? <Badge size="xs" variant="light" color={kind === "path" ? "violet" : kind === "template" ? "blue" : "gray"}>{t(`solutions.designer.valueKind.${kind}`)}</Badge> : null;
  const common = {
    size: "xs" as const,
    label: <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>{label}{badge}</span>,
    required,
    value: text,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setText(e.currentTarget.value),
    onBlur: () => { if (text !== shown) commit(text); },
    rightSection: menu,
  };
  return multiline ? <Textarea {...common} autosize minRows={2} maxRows={8} /> : <TextInput {...common} />;
}

function str(v: ConfigValue | undefined): string {
  return v === undefined || v === null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
}

function SettingField({ name, spec, value, catalog, onChange }: {
  name: string;
  spec: SettingSpec;
  value: ConfigValue | undefined;
  catalog: DesignerCatalog;
  onChange: (v: ConfigValue | undefined) => void;
}) {
  const { t } = useI18n();
  const key = `solutions.designer.setting.${name}`;
  const label = t(key) === key ? spec.label ?? name : t(key);
  if (spec.type === "bool") return <Switch size="xs" label={label} checked={value === true} onChange={(e) => onChange(e.currentTarget.checked || undefined)} />;
  if (spec.type === "int") return <NumberInput size="xs" label={label} required={spec.required} value={typeof value === "number" ? value : ""} placeholder={spec.default !== undefined ? String(spec.default) : undefined} onChange={(v) => onChange(v === "" ? undefined : Number(v))} />;
  if (spec.type === "collection") return <Select size="xs" label={label} required={spec.required} searchable data={Object.entries(catalog.collections).map(([id, c]) => ({ value: id, label: `${c.title} (${id})` }))} value={str(value) || null} onChange={(v) => onChange(v ?? undefined)} />;
  if (spec.type === "space") return <Select size="xs" label={label} required={spec.required} searchable data={catalog.spaces.map((s) => ({ value: s.id, label: `${s.title} (${s.id})` }))} value={str(value) || null} onChange={(v) => onChange(v ?? undefined)} />;
  if (spec.type === "choice") return <Select size="xs" label={label} required={spec.required} data={spec.choices ?? []} value={str(value) || null} onChange={(v) => onChange(v ?? undefined)} />;
  return <TextInput size="xs" label={label} required={spec.required} value={str(value)} placeholder={spec.default !== undefined ? String(spec.default) : undefined} onChange={(e) => onChange(e.currentTarget.value || undefined)} />;
}

function setKey(config: StepConfig, key: string, value: ConfigValue | undefined): StepConfig {
  const next = { ...config };
  if (value === undefined || value === "") delete next[key];
  else next[key] = value;
  return next;
}

export function StepSettings({ node, graph, contract, catalog, solution, templates, onApply, onDelete, onContract, onOpen, onCreate }: {
  node: GraphNode;
  graph: ProcessGraph;
  contract: ContractInput[];
  catalog: DesignerCatalog | null;
  solution: SolutionRow | undefined;
  templates: TemplateRow[];
  onApply: (patch: Partial<GraphNode>) => void;
  onDelete: () => void;
  onContract: (inputs: ContractInput[]) => void;
  onOpen: (kind: "ai" | "decision" | "tool", id: string) => void;
  onCreate: (kind: "ai" | "decision") => void;
}) {
  const { t } = useI18n();
  const [label, setLabel] = useState(node.label);
  const [ref, setRef] = useState(node.ref ?? "");
  const [source, setSource] = useState(node.source ?? "manual");
  const [seconds, setSeconds] = useState<number>(node.seconds ?? 3600);
  const [config, setConfig] = useState<StepConfig>(node.config ?? {});
  const [loop, setLoop] = useState(node.loop ?? null);
  const [deadline, setDeadline] = useState<number | null>(node.deadline ?? null);
  const [inputs, setInputs] = useState<ContractInput[]>(contract);
  useEffect(() => {
    setLabel(node.label); setRef(node.ref ?? ""); setSource(node.source ?? "manual"); setSeconds(node.seconds ?? 3600);
    setConfig(node.config ?? {}); setLoop(node.loop ?? null); setDeadline(node.deadline ?? null);
  }, [node]);
  useEffect(() => setInputs(contract), [contract]);
  const paths = useMemo(() => (catalog ? dataPaths(graph, node.id, catalog, inputs) : []), [catalog, graph, node.id, inputs]);
  if (!catalog) return <Text size="xs" c="dimmed">…</Text>;

  const action = node.kind === "action" ? catalog.actions.find((a) => a.id === ref) : undefined;
  const tool = node.kind === "action" ? catalog.tools.find((x) => x.id === ref) : undefined;
  const bound = (config.inputs ?? {}) as Record<string, string>;
  let inputSpecs: Record<string, SettingSpec> = action?.inputs ?? tool?.inputs ?? {};
  if (action?.inputs_from === "collection" && typeof config.collection === "string") {
    const coll = catalog.collections[config.collection];
    inputSpecs = Object.fromEntries((coll?.fields ?? []).map((f) => [f.name, { label: f.label, required: f.required }]));
  }
  // bound inputs the schema does not know stay visible (imported or hand-written)
  for (const k of Object.keys(bound)) if (!(k in inputSpecs)) inputSpecs = { ...inputSpecs, [k]: { label: k } };
  const setInput = (name: string, value: string) => {
    const next = { ...bound };
    if (value) next[name] = value; else delete next[name];
    setConfig({ ...config, inputs: next });
  };
  const trigger = catalog.triggers.find((x) => x.id === source);
  // The core's own actions are labelled here (four languages); a
  // collection's fields and a tool's inputs bring their own labels.
  const settingLabel = (name: string, fallback?: string) => {
    const key = `solutions.designer.setting.${name}`;
    const text = action && !action.inputs_from ? t(key) : key;
    const shown = text !== key ? text : fallback ?? name;
    return shown && shown.toLowerCase() !== name.toLowerCase().replace(/_/g, " ") ? `${name} – ${shown}` : name;
  };
  const options = bindingOptions(node.kind, solution, templates);
  const allOptions = node.kind === "action" ? [...new Set([...options, ...catalog.actions.map((a) => a.id), ...catalog.tools.map((x) => x.id)])] : options;
  const outputs = node.kind === "ai" ? catalog.briefings.find((b) => b.id === ref)?.outputs : action?.outputs ?? tool?.outputs;

  const apply = () => {
    const patch: Partial<GraphNode> = { label, ref: ref || undefined };
    if (node.kind === "start") patch.source = source;
    if (node.kind === "timer") patch.seconds = seconds;
    if (["start", "gate", "action", "decision", "call", "timer"].includes(node.kind)) patch.config = Object.keys(config).length ? config : null;
    if (node.kind === "ai" || node.kind === "action") patch.loop = loop && loop.over && loop.as ? loop : null;
    if (node.kind === "gate") patch.deadline = deadline;
    onApply(patch);
    if (node.kind === "start" && JSON.stringify(inputs) !== JSON.stringify(contract)) onContract(inputs);
  };

  return (
    <Stack gap="xs">
      <Group gap={6} justify="space-between" wrap="nowrap">
        <Group gap={6} wrap="nowrap">
          <span style={{ color: KIND_STYLE[node.kind].color, fontSize: 16 }}>{KIND_STYLE[node.kind].glyph}</span>
          <Text fw={700} size="sm">{t(`solutions.card.kind.${node.kind}`)}</Text>
          <Text size="xs" c="dimmed">{node.id}</Text>
        </Group>
        {(action?.effect === "external" || tool?.effect === "external") && <Badge size="xs" color="orange">{t("solutions.designer.needsGate")}</Badge>}
      </Group>
      <TextInput size="xs" label={t("solutions.card.label")} value={label} onChange={(e) => setLabel(e.currentTarget.value)} />

      {["ai", "decision", "action", "call", "end"].includes(node.kind) && node.ref !== "inline" && (
        <Group gap={6} align="flex-end" wrap="nowrap">
          <Select style={{ flex: 1 }} size="xs" searchable clearable={node.kind === "end"} label={t(`solutions.card.ref_${node.kind}`)} data={allOptions} value={ref || null} onChange={(v) => { setRef(v ?? ""); if (node.kind === "action") setConfig({ inputs: {} }); }} nothingFoundMessage={t("solutions.card.noOptions")} />
          {node.kind === "ai" && ref && <Button size="compact-xs" variant="subtle" onClick={() => onOpen("ai", ref)}>{t("solutions.designer.open")}</Button>}
          {node.kind === "decision" && ref && <Button size="compact-xs" variant="subtle" onClick={() => onOpen("decision", ref)}>{t("solutions.designer.open")}</Button>}
          {node.kind === "action" && tool && <Button size="compact-xs" variant="subtle" onClick={() => onOpen("tool", ref)}>{t("solutions.designer.open")}</Button>}
          {(node.kind === "ai" || node.kind === "decision") && <Button size="compact-xs" variant="subtle" onClick={() => onCreate(node.kind as "ai" | "decision")}>{t("solutions.designer.new")}</Button>}
        </Group>
      )}
      {node.ref === "inline" && <Text size="xs" c="dimmed">{t("solutions.card.inline")}</Text>}

      {node.kind === "start" && (
        <>
          <Select size="xs" label={t("solutions.card.source")} data={catalog.triggers.map((x) => ({ value: x.id, label: t(`solutions.new.source_${x.id}`) }))} value={source} onChange={(v) => { if (v) { setSource(v); setConfig(v === "schedule" ? { every: "day", at: "08:00" } : {}); } }} />
          {Object.entries(trigger?.config ?? {}).map(([k, spec]) => (
            <SettingField key={k} name={k} spec={spec} value={config[k]} catalog={catalog} onChange={(v) => setConfig(setKey(config, k, v))} />
          ))}
          <Divider label={t("solutions.designer.startForm")} labelPosition="left" />
          <Text size="xs" c="dimmed">{t("solutions.designer.startFormHint")}</Text>
          {inputs.map((c, i) => (
            <Group key={i} gap={4} wrap="nowrap" align="flex-end">
              <TextInput size="xs" w={110} label={i === 0 ? t("solutions.designer.field") : undefined} value={c.name} error={c.name && !/^[a-z][a-z0-9_]*$/.test(c.name) ? " " : undefined} onChange={(e) => setInputs(inputs.map((x, j) => (j === i ? { ...x, name: e.currentTarget.value } : x)))} />
              <TextInput size="xs" style={{ flex: 1 }} label={i === 0 ? t("solutions.designer.fieldDescription") : undefined} value={c.description} onChange={(e) => setInputs(inputs.map((x, j) => (j === i ? { ...x, description: e.currentTarget.value } : x)))} />
              <TextInput size="xs" w={80} label={i === 0 ? t("solutions.designer.fieldDefault") : undefined} value={c.default ?? ""} placeholder={t("solutions.designer.required")} onChange={(e) => setInputs(inputs.map((x, j) => (j === i ? { ...x, default: e.currentTarget.value === "" ? undefined : e.currentTarget.value } : x)))} />
              <ActionIcon size="sm" variant="subtle" color="red" onClick={() => setInputs(inputs.filter((_, j) => j !== i))}>✕</ActionIcon>
            </Group>
          ))}
          <Button size="compact-xs" variant="light" onClick={() => setInputs([...inputs, { name: `field${inputs.length + 1}`, description: "" }])}>{t("solutions.designer.addField")}</Button>
        </>
      )}

      {node.kind === "gate" && (
        <>
          <TagsInput size="xs" label={t("solutions.designer.approving")} description={t("solutions.designer.approvingHint")} value={str(config.approving).split(",").map((x) => x.trim()).filter(Boolean)} onChange={(v) => setConfig(setKey(config, "approving", v.join(", ")))} />
          <NumberInput size="xs" label={t("solutions.designer.deadline")} description={t("solutions.designer.deadlineHint")} min={0} value={deadline ? Math.round(deadline / 3600) : ""} onChange={(v) => setDeadline(v === "" || Number(v) === 0 ? null : Number(v) * 3600)} />
          <TagsInput size="xs" label={t("solutions.designer.rule")} description={t("solutions.designer.ruleHint")} data={paths.map((p) => p.path)} value={str(config.rule).split(",").map((x) => x.trim()).filter(Boolean)} onChange={(v) => setConfig(setKey(config, "rule", v.join(", ")))} />
        </>
      )}

      {node.kind === "timer" && (
        <NumberInput size="xs" label={t("solutions.designer.waitMinutes")} min={1} value={Math.round(seconds / 60)} onChange={(v) => setSeconds(Math.max(1, Number(v) || 1) * 60)} />
      )}

      {node.kind === "action" && action && Object.keys(action.config).length > 0 && (
        Object.entries(action.config).map(([k, spec]) => (
          <SettingField key={k} name={k} spec={spec} value={config[k]} catalog={catalog} onChange={(v) => setConfig(setKey(config, k, v))} />
        ))
      )}
      {node.kind === "action" && Object.keys(inputSpecs).length > 0 && (
        <>
          <Divider label={t("solutions.designer.inputs")} labelPosition="left" />
          {Object.entries(inputSpecs).map(([name, spec]) => (
            <ValueField key={name} label={settingLabel(name, spec.label)} required={spec.required} multiline={spec.multiline} value={bound[name] ?? ""} paths={paths} onChange={(v) => setInput(name, v)} />
          ))}
        </>
      )}

      {node.kind === "decision" && ref && (
        <Text size="xs" c="dimmed">{t("solutions.designer.decisionReads", { inputs: catalog.decisions.find((d) => d.id === ref)?.inputs.join(", ") || "–" })}</Text>
      )}

      {(node.kind === "ai" || node.kind === "action") && (
        <>
          <Divider label={t("solutions.designer.forEach")} labelPosition="left" />
          <Group gap={4} grow>
            <Select size="xs" clearable searchable label={t("solutions.designer.over")} data={[...new Set([...paths.filter((p) => p.path !== "each").map((p) => p.path), ...(loop?.over ? [loop.over] : [])])]} value={loop?.over ?? null} onChange={(v) => setLoop(v ? { over: v, as: loop?.as || "item", ...(loop?.max ? { max: loop.max } : {}) } : null)} />
            <TextInput size="xs" label={t("solutions.designer.as")} disabled={!loop} value={loop?.as ?? ""} onChange={(e) => loop && setLoop({ ...loop, as: e.currentTarget.value })} />
            <NumberInput size="xs" label={t("solutions.designer.max")} disabled={!loop} value={loop?.max ?? ""} onChange={(v) => loop && setLoop({ ...loop, max: v === "" ? undefined : Number(v) })} />
          </Group>
        </>
      )}

      {outputs && Object.keys(outputs).length > 0 && (
        <Text size="xs" c="dimmed">{t("solutions.designer.outputs", { names: Object.keys(outputs).join(", ") })}</Text>
      )}
      <Text size="xs" c="dimmed">{t("solutions.designer.outcomes", { names: (catalog.outcomes[node.kind] ?? []).join(", ") || "–" })}</Text>

      <Group gap="xs">
        <Button size="xs" onClick={apply}>{t("solutions.card.apply")}</Button>
        <Button size="xs" variant="subtle" color="red" onClick={onDelete}>{t("solutions.card.delete")}</Button>
      </Group>
    </Stack>
  );
}

// -- edges: outcome and, out of a deciding gateway, its conditions (ADR-0056) --

const OPS = ["eq", "ne", "gt", "ge", "lt", "le", "in", "raw"] as const;
type Op = (typeof OPS)[number];

function quote(value: string): string {
  const v = value.trim();
  if (/^-?\d+(\.\d+)?$/.test(v) || v === "true" || v === "false") return v;
  return `"${v.replace(/"/g, "")}"`;
}

/** An S-FEEL unary test from a comparison the person picked. */
export function composeTest(op: Op, value: string): string {
  const v = value.trim();
  if (op === "raw") return v;
  if (op === "eq") return quote(v);
  if (op === "ne") return `not(${quote(v)})`;
  if (op === "in") return v.split(",").map((x) => quote(x)).filter((x) => x !== '""').join(", ");
  return `${{ gt: ">", ge: ">=", lt: "<", le: "<=" }[op]} ${v}`;
}

/** The other way round, for showing a test that is already there. */
export function parseTest(test: string): { op: Op; value: string } {
  const t = test.trim();
  const unq = (x: string) => x.trim().replace(/^"(.*)"$/, "$1");
  let m = /^not\((.*)\)$/.exec(t);
  if (m) return { op: "ne", value: unq(m[1] ?? "") };
  m = /^(>=|<=|>|<)\s*(.+)$/.exec(t);
  if (m) return { op: ({ ">": "gt", ">=": "ge", "<": "lt", "<=": "le" } as Record<string, Op>)[m[1] ?? ">"] ?? "raw", value: (m[2] ?? "").trim() };
  if (/^"[^"]*"(\s*,\s*"[^"]*")+$/.test(t)) return { op: "in", value: t.split(",").map(unq).join(", ") };
  if (/^"[^"]*"$/.test(t) || /^-?\d+(\.\d+)?$/.test(t) || t === "true" || t === "false") return { op: "eq", value: unq(t) };
  return { op: "raw", value: t };
}

export function EdgeSettings({ edge, graph, catalog, contract, onApply, onDelete }: {
  edge: GraphEdge;
  graph: ProcessGraph;
  catalog: DesignerCatalog | null;
  contract: ContractInput[];
  onApply: (patch: { outcome?: string; when?: GraphEdge["when"] | null; default?: boolean }) => void;
  onDelete: () => void;
}) {
  const { t } = useI18n();
  const source = graph.nodes.find((n) => n.id === edge.source);
  const deciding = source?.kind === "xor";
  const [outcome, setOutcome] = useState(edge.outcome);
  const [match, setMatch] = useState<"all" | "any">(edge.when?.match ?? "all");
  const [rows, setRows] = useState(() => (edge.when?.conditions ?? []).map((c) => ({ input: c.input, ...parseTest(c.test) })));
  const [otherwise, setOtherwise] = useState(!!edge.default);
  useEffect(() => {
    setOutcome(edge.outcome); setMatch(edge.when?.match ?? "all"); setOtherwise(!!edge.default);
    setRows((edge.when?.conditions ?? []).map((c) => ({ input: c.input, ...parseTest(c.test) })));
  }, [edge]);
  const paths = useMemo(() => (catalog && source ? dataPaths(graph, source.id, catalog, contract) : []), [catalog, graph, source, contract]);
  const validOutcome = outcome === "" || /^[a-z][a-z0-9_]*$/.test(outcome);
  const apply = () => {
    const conditions = rows.filter((r) => r.input && r.value.trim()).map((r) => ({ input: r.input, test: composeTest(r.op, r.value) }));
    onApply({
      outcome,
      ...(deciding ? { when: otherwise || !conditions.length ? null : { match, conditions }, default: otherwise } : {}),
    });
  };
  return (
    <Stack gap="xs">
      <Text fw={700} size="sm">{t("solutions.card.edge")}</Text>
      <Text size="xs" c="dimmed">{edge.source} → {edge.target}</Text>
      <TextInput size="xs" label={t("solutions.card.outcome")} description={source?.kind === "parallel" ? t("solutions.card.outcomeParallel") : t("solutions.card.outcomeHelp")} value={outcome} error={!validOutcome ? t("solutions.card.idInvalid") : undefined} onChange={(e) => setOutcome(e.currentTarget.value)} />
      {deciding && (
        <>
          <Divider label={t("solutions.designer.condition")} labelPosition="left" />
          <Switch size="xs" label={t("solutions.designer.otherwise")} checked={otherwise} onChange={(e) => setOtherwise(e.currentTarget.checked)} />
          {!otherwise && (
            <>
              {rows.length > 1 && (
                <Select size="xs" label={t("solutions.designer.match")} data={[{ value: "all", label: t("solutions.designer.matchAll") }, { value: "any", label: t("solutions.designer.matchAny") }]} value={match} onChange={(v) => setMatch((v as "all" | "any") ?? "all")} />
              )}
              {rows.map((r, i) => (
                <Stack key={i} gap={4} p={6} style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 6 }}>
                  <Select size="xs" searchable label={t("solutions.designer.value")} data={[...new Set([...paths.map((p) => p.path), ...rows.map((x) => x.input).filter(Boolean)])]} value={r.input || null} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, input: v ?? "" } : x)))} />
                  <Group gap={4} wrap="nowrap" align="flex-end">
                    <Select size="xs" w={130} data={OPS.map((o) => ({ value: o, label: t(`solutions.designer.op.${o}`) }))} value={r.op} onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, op: (v as Op) ?? "eq" } : x)))} />
                    <TextInput size="xs" style={{ flex: 1 }} placeholder={r.op === "in" ? "a, b" : r.op === "raw" ? '"ok", > 0' : ""} value={r.value} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, value: e.currentTarget.value } : x)))} />
                    <ActionIcon size="sm" variant="subtle" color="red" onClick={() => setRows(rows.filter((_, j) => j !== i))}>✕</ActionIcon>
                  </Group>
                </Stack>
              ))}
              <Button size="compact-xs" variant="light" onClick={() => setRows([...rows, { input: "", op: "eq", value: "" }])}>{t("solutions.designer.addCondition")}</Button>
            </>
          )}
        </>
      )}
      <Group gap="xs">
        <Button size="xs" disabled={!validOutcome} onClick={apply}>{t("solutions.card.apply")}</Button>
        <Button size="xs" variant="subtle" color="red" onClick={onDelete}>{t("solutions.card.deleteEdge")}</Button>
      </Group>
    </Stack>
  );
}
