// Card-level editing for the canvas (ADR-0040 §5): what a card is bound
// to (briefing, decision, action, process, chain target, trigger source,
// timer) and what an edge is called. Step *properties* stay in StepPanel;
// this is the structural part - label, binding, outcome name.
import { Button, Group, Modal, NumberInput, Select, Stack, Text, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";
import type { GraphNode, NodeKind, SolutionRow, TemplateRow } from "../api/client";
import { useI18n } from "../i18n";
import { KINDS, KIND_STYLE } from "./canvasKinds";

const ID = /^[a-z][a-z0-9_]*$/;
const INTERNAL_ACTIONS = ["export", "archive", "dispatch", "forward", "join", "record", "knowledge", "rows"];
const SOURCES = ["manual", "chain", "folder", "mailbox", "schedule"];

export function bindingOptions(kind: NodeKind, solution: SolutionRow | undefined, templates: TemplateRow[]): string[] {
  if (!solution) return [];
  if (kind === "ai") return solution.tree.ai;
  if (kind === "decision") return solution.tree.decisions;
  if (kind === "action") return [...solution.tree.tools, ...INTERNAL_ACTIONS];
  if (kind === "call" || kind === "end") return templates.map((t) => t.id).filter((id) => id !== "document-intake" && id !== "mailroom-assign");
  return [];
}

function BindingField({ kind, value, options, onChange }: { kind: NodeKind; value: string; options: string[]; onChange: (v: string) => void }) {
  const { t } = useI18n();
  if (!["ai", "decision", "action", "call", "end"].includes(kind)) return null;
  const required = kind !== "end";
  return (
    <Select
      label={t(`solutions.card.ref_${kind}`)}
      data={options}
      value={value || null}
      searchable
      clearable={!required}
      required={required}
      nothingFoundMessage={t("solutions.card.noOptions")}
      onChange={(v) => onChange(v ?? "")}
    />
  );
}

export function NewCardDialog({ kind, solution, templates, taken, onClose, onCreate }: {
  kind: NodeKind | null;
  solution: SolutionRow | undefined;
  templates: TemplateRow[];
  taken: string[];
  onClose: () => void;
  onCreate: (node: Omit<GraphNode, "x" | "y" | "w" | "h">) => void;
}) {
  const { t } = useI18n();
  const [id, setId] = useState("");
  const [label, setLabel] = useState("");
  const [ref, setRef] = useState("");
  const [source, setSource] = useState("manual");
  const [seconds, setSeconds] = useState<number>(3600);
  useEffect(() => {
    if (!kind) return;
    let base = kind === "xor" ? "choice" : kind === "parallel" ? "split" : kind === "end" ? "finish" : kind; // "end" is reserved
    let candidate = base;
    let n = 2;
    while (taken.includes(candidate)) { candidate = `${base}${n}`; n += 1; }
    base = candidate;
    setId(base); setLabel(""); setRef(""); setSource("manual"); setSeconds(3600);
  }, [kind, taken]);
  if (!kind) return null;
  const options = bindingOptions(kind, solution, templates);
  const needsRef = ["ai", "decision", "action", "call"].includes(kind);
  const valid = ID.test(id) && !taken.includes(id) && (!needsRef || !!ref);
  return (
    <Modal opened onClose={onClose} title={<Group gap={6}><span style={{ color: KIND_STYLE[kind].color }}>{KIND_STYLE[kind].glyph}</span><span>{t("solutions.card.add", { kind: t(`solutions.card.kind.${kind}`) })}</span></Group>}>
      <Stack>
        <Text size="xs" c="dimmed">{t(`solutions.card.hint.${kind}`)}</Text>
        <TextInput label={t("solutions.card.id")} description={t("solutions.card.idHelp")} value={id} required error={id && (!ID.test(id) || taken.includes(id)) ? t("solutions.card.idInvalid") : undefined} onChange={(e) => setId(e.currentTarget.value)} />
        <TextInput label={t("solutions.card.label")} value={label} onChange={(e) => setLabel(e.currentTarget.value)} />
        <BindingField kind={kind} value={ref} options={options} onChange={setRef} />
        {kind === "start" && <Select label={t("solutions.card.source")} data={SOURCES.map((s) => ({ value: s, label: t(`solutions.new.source_${s}`) }))} value={source} onChange={(v) => v && setSource(v)} />}
        {kind === "timer" && <NumberInput label={t("solutions.card.seconds")} value={seconds} min={1} onChange={(v) => setSeconds(Number(v) || 1)} />}
        <Group justify="flex-end">
          <Button variant="subtle" onClick={onClose}>{t("solutions.cancel")}</Button>
          <Button disabled={!valid} onClick={() => onCreate({ id, kind, label, ref: ref || undefined, source: kind === "start" ? source : undefined, seconds: kind === "timer" ? seconds : undefined })}>{t("solutions.card.place")}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function CardEditor({ node, solution, templates, onApply, onDelete }: {
  node: GraphNode;
  solution: SolutionRow | undefined;
  templates: TemplateRow[];
  onApply: (patch: Partial<GraphNode>) => void;
  onDelete: () => void;
}) {
  const { t } = useI18n();
  const [label, setLabel] = useState(node.label);
  const [ref, setRef] = useState(node.ref ?? "");
  const [source, setSource] = useState(node.source ?? "manual");
  const [seconds, setSeconds] = useState<number>(node.seconds ?? 3600);
  useEffect(() => { setLabel(node.label); setRef(node.ref ?? ""); setSource(node.source ?? "manual"); setSeconds(node.seconds ?? 3600); }, [node]);
  const options = bindingOptions(node.kind, solution, templates);
  const dirty = label !== node.label || ref !== (node.ref ?? "") || source !== (node.source ?? "manual") || seconds !== (node.seconds ?? 3600);
  return (
    <Stack gap="xs">
      <Group gap={6}>
        <span style={{ color: KIND_STYLE[node.kind].color, fontSize: 16 }}>{KIND_STYLE[node.kind].glyph}</span>
        <Text fw={700} size="sm">{t(`solutions.card.kind.${node.kind}`)}</Text>
        <Text size="xs" c="dimmed">{node.id}</Text>
      </Group>
      <TextInput size="xs" label={t("solutions.card.label")} value={label} onChange={(e) => setLabel(e.currentTarget.value)} />
      {node.ref === "inline" && node.kind === "ai" ? (
        <Text size="xs" c="dimmed">{t("solutions.card.inline")}</Text>
      ) : (
        <BindingField kind={node.kind} value={ref} options={options} onChange={setRef} />
      )}
      {node.kind === "start" && <Select size="xs" label={t("solutions.card.source")} data={SOURCES.map((s) => ({ value: s, label: t(`solutions.new.source_${s}`) }))} value={source} onChange={(v) => v && setSource(v)} />}
      {node.kind === "timer" && <NumberInput size="xs" label={t("solutions.card.seconds")} value={seconds} min={1} onChange={(v) => setSeconds(Number(v) || 1)} />}
      <Group gap="xs">
        <Button size="xs" disabled={!dirty} onClick={() => onApply({ label, ref, source: node.kind === "start" ? source : undefined, seconds: node.kind === "timer" ? seconds : undefined })}>{t("solutions.card.apply")}</Button>
        <Button size="xs" variant="subtle" color="red" onClick={onDelete}>{t("solutions.card.delete")}</Button>
      </Group>
    </Stack>
  );
}

export function EdgeEditor({ outcome, sourceKind, onApply, onDelete }: {
  outcome: string;
  sourceKind: NodeKind | undefined;
  onApply: (outcome: string) => void;
  onDelete: () => void;
}) {
  const { t } = useI18n();
  const [value, setValue] = useState(outcome);
  useEffect(() => setValue(outcome), [outcome]);
  const valid = value === "" || ID.test(value);
  return (
    <Stack gap="xs">
      <Text fw={700} size="sm">{t("solutions.card.edge")}</Text>
      <TextInput size="xs" label={t("solutions.card.outcome")} description={sourceKind === "parallel" ? t("solutions.card.outcomeParallel") : t("solutions.card.outcomeHelp")} value={value} error={!valid ? t("solutions.card.idInvalid") : undefined} onChange={(e) => setValue(e.currentTarget.value)} />
      <Group gap="xs">
        <Button size="xs" disabled={!valid || value === outcome} onClick={() => onApply(value)}>{t("solutions.card.apply")}</Button>
        <Button size="xs" variant="subtle" color="red" onClick={onDelete}>{t("solutions.card.deleteEdge")}</Button>
      </Group>
    </Stack>
  );
}

export const CARD_KINDS = KINDS;
