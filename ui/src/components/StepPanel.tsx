// The workshop's property panel (ADR-0034 §6, Stufe 1 "Politur"): what a
// selected step declares, as a form rather than as TOML - and what it did
// last in this instance. Briefings are edited here as fields; the core
// serializes them back to ai/<id>.aimn.toml and validates the whole
// solution before anything is reloaded.
import { Badge, Button, Code, Divider, Group, NumberInput, Select, Stack, Table, Tabs, Text, TextInput, Textarea } from "@mantine/core";
import { useEffect, useState } from "react";
import type { BriefingFields, ElementStep, SolutionElement, SolutionRow, StepHistory } from "../api/client";
import { useI18n } from "../i18n";
import type { NodeBadge } from "./Diagrams";

export const PROMPT_LANGUAGES = ["en", "de", "fr", "it"] as const;

/** Declared facts of a step as chips for the canvas. */
export type BadgeLabels = Record<"classify" | "assign" | "unbound" | "internal" | "read" | "external" | "mandate" | "categorization" | "deadline" | "waitsFor" | "each" | "client", string>;

export function stepBadges(step: ElementStep, solution: SolutionRow | undefined, labels: BadgeLabels): NodeBadge[] {
  const out: NodeBadge[] = [];
  if (step.join_expects) {
    out.push({ text: `${labels.waitsFor} ${step.join_expects.length}`, color: "blue", title: step.join_expects.join(", ") });
    return out;
  }
  if (step.for_each) out.push({ text: `${labels.each} ${step.for_each.over}`, color: "violet", title: `${step.for_each.as}${step.for_each.max ? ` · max ${step.for_each.max}` : ""}` });
  if (step.kind === "agent_step") {
    if (step.mechanics) out.push({ text: labels[step.mechanics], color: "gray" });
    if (step.model_role) {
      const bound = solution?.model_roles[step.model_role];
      out.push({ text: `${step.model_role} → ${bound?.model ?? labels.unbound}`, color: bound?.model ? "violet" : "red", title: bound?.model_ref ?? bound?.reason ?? "" });
    }
    if (step.data_class) out.push({ text: step.data_class, color: step.data_class === "sensitive" ? "red" : step.data_class === "confidential" ? "orange" : "gray" });
  } else if (step.kind === "human_gate") {
    if (step.deadline_seconds) out.push({ text: formatDuration(step.deadline_seconds), color: "yellow", title: labels.deadline });
    if (step.categorization) out.push({ text: labels.categorization, color: "gray" });
  } else if (step.kind === "system_action") {
    if (step.effect === "internal") out.push({ text: labels.internal, color: "gray" });
    if (step.effect === "read") out.push({ text: labels.read, color: "blue" });
    else if (step.exec === "client") out.push({ text: step.tool_mode === "live" ? labels.client : `${labels.client} · ${step.tool_mode}`, color: step.tool_mode === "live" ? "violet" : "blue" });
    else out.push({ text: step.tool_mode === "live" ? labels.external : `${labels.external} · ${step.tool_mode}`, color: step.tool_mode === "live" ? "orange" : "blue" });
    if (step.mandate) out.push({ text: labels.mandate, color: "teal" });
    if (step.process) out.push({ text: `→ ${step.process}`, color: "blue" });
  }
  return out;
}

export function formatDuration(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}min`;
  return `${seconds}s`;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Table.Tr>
      <Table.Td w={130} style={{ verticalAlign: "top" }}><Text size="xs" c="dimmed">{label}</Text></Table.Td>
      <Table.Td><Text size="sm" component="div">{children}</Text></Table.Td>
    </Table.Tr>
  );
}

export function LastRun({ history, stats }: { history: StepHistory | undefined; stats?: number }) {
  const { t, formatDate } = useI18n();
  if (!history || history.runs === 0) return <Text size="sm" c="dimmed">{t("solutions.panel.neverRan")}</Text>;
  return (
    <Table withRowBorders={false} verticalSpacing={4}>
      <Table.Tbody>
        <Row label={t("solutions.panel.runs")}>{history.runs}{history.open > 0 ? ` (${t("solutions.panel.open", { n: history.open })})` : ""}{history.instances ? ` · ${t("solutions.panel.instances", { n: history.instances })}` : ""}{stats !== undefined && stats !== history.runs ? ` · ${stats}` : ""}</Row>
        <Row label={t("solutions.panel.lastAt")}>{history.last_at ? formatDate(history.last_at) : "–"}</Row>
        <Row label={t("solutions.panel.lastOutcome")}>
          {history.last_outcome ? <Badge size="sm" color={history.last_outcome === "failed" ? "red" : "teal"} variant="light">{history.last_outcome}</Badge> : <Badge size="sm" color="gray" variant="light">{history.last_status}</Badge>}
        </Row>
        <Row label={t("solutions.panel.distribution")}>
          <Group gap={4}>{Object.entries(history.outcomes).map(([o, n]) => <Badge key={o} size="xs" variant="outline" color={o === "failed" ? "red" : "gray"}>{o} {n}</Badge>)}</Group>
        </Row>
        {history.last_error && <Row label={t("solutions.panel.lastError")}><Text size="xs" c="red">{history.last_error}</Text></Row>}
        {history.last_case && <Row label={t("solutions.panel.lastCase")}><Code>{history.last_case.slice(0, 12)}</Code></Row>}
      </Table.Tbody>
    </Table>
  );
}

function fieldsOf(summary: Record<string, unknown>): BriefingFields {
  const table = (v: unknown) => (v && typeof v === "object" ? { ...(v as Record<string, string>) } : {});
  return {
    role: table(summary.role),
    instructions: table(summary.instructions),
    limits: ((summary.limits as Record<string, string>[]) ?? []).map((l) => l.en ?? Object.values(l)[0] ?? ""),
    model_role: String(summary.model_role ?? ""),
    flexibility: String(summary.flexibility ?? "strict"),
    skills: [...((summary.skills as string[]) ?? [])],
    tools: [...((summary.tools as string[]) ?? [])],
    output: table(summary.output),
    confidence_threshold: (summary.confidence_threshold as number | null) ?? null,
  };
}

/** A briefing as a form. Read-only when `onSave` is absent. */
export function BriefingForm({ element, solution, onSave, busy }: { element: SolutionElement; solution?: SolutionRow; onSave?: (fields: BriefingFields) => Promise<void>; busy?: boolean }) {
  const { t } = useI18n();
  const [fields, setFields] = useState<BriefingFields>(() => fieldsOf(element.summary));
  const [dirty, setDirty] = useState(false);
  const [lang, setLang] = useState<(typeof PROMPT_LANGUAGES)[number]>("en");
  useEffect(() => { setFields(fieldsOf(element.summary)); setDirty(false); }, [element]);
  const editable = Boolean(onSave);
  const update = (patch: Partial<BriefingFields>) => { setFields((f) => ({ ...f, ...patch })); setDirty(true); };
  const roles = Object.keys(solution?.model_roles ?? {});
  const binding = fields.model_role ? solution?.model_roles[fields.model_role] : undefined;
  const promptHash = String(element.summary.prompt_hash ?? "");
  return (
    <Stack gap="xs">
      <Group gap="xs" justify="space-between">
        <Group gap={4}>
          {PROMPT_LANGUAGES.map((l) => (
            <Button key={l} size="compact-xs" variant={lang === l ? "filled" : "subtle"} onClick={() => setLang(l)}>{l}{fields.instructions[l] || fields.role[l] ? "" : " ·"}</Button>
          ))}
        </Group>
        {editable && <Button size="xs" disabled={!dirty} loading={busy} onClick={() => onSave!(fields).then(() => setDirty(false))}>{t("solutions.save")}</Button>}
      </Group>
      <Textarea label={t("solutions.briefing.role")} autosize minRows={1} maxRows={4} value={fields.role[lang] ?? ""} readOnly={!editable} onChange={(e) => update({ role: { ...fields.role, [lang]: e.currentTarget.value } })} description={lang === "en" ? t("solutions.panel.englishIsDefault") : undefined} />
      <Textarea label={t("solutions.briefing.instructions")} autosize minRows={3} maxRows={12} value={fields.instructions[lang] ?? ""} readOnly={!editable} onChange={(e) => update({ instructions: { ...fields.instructions, [lang]: e.currentTarget.value } })} />
      <Textarea label={t("solutions.briefing.limits")} description={t("solutions.panel.onePerLine")} autosize minRows={1} maxRows={8} value={fields.limits.join("\n")} readOnly={!editable} onChange={(e) => update({ limits: e.currentTarget.value.split("\n") })} />
      <Group grow align="flex-start">
        <Select label={t("solutions.briefing.modelRole")} data={roles.length ? roles : fields.model_role ? [fields.model_role] : []} value={fields.model_role || null} readOnly={!editable} onChange={(v) => update({ model_role: v ?? "" })} clearable={editable} description={binding ? (binding.model ? `→ ${binding.model} (${binding.model_ref ?? ""})` : binding.reason ?? t("solutions.unbound")) : undefined} error={binding && !binding.model ? t("solutions.unbound") : undefined} />
        <Select label={t("solutions.briefing.flexibility")} data={[{ value: "strict", label: t("solutions.panel.flex.strict") }, { value: "guided", label: t("solutions.panel.flex.guided") }, { value: "open", label: t("solutions.panel.flex.open") }]} value={fields.flexibility} readOnly={!editable} onChange={(v) => v && update({ flexibility: v })} />
      </Group>
      <Group grow align="flex-start">
        <TextInput label={t("solutions.briefing.skills")} value={fields.skills.join(", ")} readOnly={!editable} onChange={(e) => update({ skills: e.currentTarget.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
        <TextInput label={t("solutions.briefing.tools")} value={fields.tools.join(", ")} readOnly={!editable} onChange={(e) => update({ tools: e.currentTarget.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
        <NumberInput label={t("solutions.panel.confidence")} min={0} max={1} step={0.05} value={fields.confidence_threshold ?? ""} readOnly={!editable} onChange={(v) => update({ confidence_threshold: typeof v === "number" ? v : null })} />
      </Group>
      <Textarea label={t("solutions.briefing.output")} description={t("solutions.panel.outputFormat")} autosize minRows={2} maxRows={10} value={Object.entries(fields.output).map(([k, v]) => `${k} = ${v}`).join("\n")} readOnly={!editable} styles={{ input: { fontFamily: "monospace", fontSize: 12 } }} onChange={(e) => {
        const output: Record<string, string> = {};
        for (const line of e.currentTarget.value.split("\n")) {
          const i = line.indexOf("=");
          if (i > 0) output[line.slice(0, i).trim()] = line.slice(i + 1).trim();
        }
        update({ output });
      }} />
      {promptHash && <Text size="xs" c="dimmed">{t("solutions.briefing.promptHash")} <Code>{promptHash.slice(0, 16)}</Code>{dirty ? ` · ${t("solutions.panel.hashChanges")}` : ""}</Text>}
    </Stack>
  );
}

/** Settings of a non-AI step: read-only facts (editing those is the modeler's job). */
export function StepFacts({ step, solution }: { step: ElementStep; solution?: SolutionRow }) {
  const { t } = useI18n();
  const outcomes = Object.entries(step.next ?? {}).map(([o, target]) => `${o} → ${target}`);
  return (
    <Table withRowBorders={false} verticalSpacing={4}>
      <Table.Tbody>
        <Row label={t("solutions.panel.kind")}><Badge size="sm" variant="outline">{t(`workshop.kind.${step.kind}`)}</Badge></Row>
        {step.source && <Row label={t("solutions.panel.source")}>{step.source}</Row>}
        {step.kind === "human_gate" && <Row label={t("solutions.panel.approving")}>{step.approving_outcomes?.length ? step.approving_outcomes.join(", ") : t("solutions.panel.none")}</Row>}
        {step.kind === "human_gate" && <Row label={t("solutions.panel.deadline")}>{step.deadline_seconds ? formatDuration(step.deadline_seconds) : "–"}</Row>}
        {step.kind === "system_action" && <Row label={t("solutions.panel.action")}><Code>{step.action}</Code>{step.tool ? ` (${t("solutions.kind.tool")} ${step.tool})` : ""}</Row>}
        {step.kind === "system_action" && step.inputs && (
          <Row label={t("solutions.panel.inputs")}>
            <Stack gap={2}>{Object.entries(step.inputs).map(([k, v]) => <Text key={k} size="xs"><b>{k}</b> ← <Code>{v}</Code></Text>)}</Stack>
          </Row>
        )}
        {step.kind === "system_action" && step.exec === "client" && <Row label={t("solutions.panel.exec")}>{t("solutions.panel.clientLong")}</Row>}
        {step.kind === "system_action" && <Row label={t("solutions.panel.effect")}><Badge size="sm" color={step.effect === "internal" ? "gray" : step.effect === "read" ? "blue" : "orange"} variant="light">{step.effect === "internal" ? t("solutions.panel.internal") : step.effect === "read" ? t("solutions.panel.read") : t("solutions.panel.external")}</Badge></Row>}
        {step.kind === "system_action" && step.effect !== "internal" && <Row label={t("solutions.panel.underConfiguration", { configuration: solution?.active_configuration ?? "standard" })}><Badge size="sm" variant="light" color={step.tool_mode === "live" ? "orange" : "blue"}>{step.tool_mode}</Badge></Row>}
        {step.kind === "system_action" && step.mandate && <Row label={t("solutions.panel.mandate")}>{t("solutions.panel.mandateYes")}</Row>}
        {step.process && <Row label={t("solutions.panel.calls")}><Code>{step.process}</Code></Row>}
        {step.decision && <Row label={t("solutions.kind.decision")}><Code>{step.decision}</Code></Row>}
        {step.timer && Object.keys(step.timer).length > 0 && <Row label={t("solutions.panel.timer")}><Code>{JSON.stringify(step.timer)}</Code></Row>}
        {step.kind === "agent_step" && step.mechanics && <Row label={t("solutions.panel.mechanics")}>{t(`solutions.panel.${step.mechanics}`)}</Row>}
        {step.kind === "agent_step" && step.data_class && <Row label={t("solutions.panel.dataClass")}>{step.data_class}</Row>}
        {step.join_expects && <Row label={t("solutions.panel.waitsFor")}><Stack gap={0}>{step.join_expects.map((h) => <Code key={h}>{h}</Code>)}</Stack></Row>}
        {step.forks && Object.entries(step.forks).map(([o, heads]) => <Row key={o} label={t("solutions.panel.splitsInto", { outcome: o })}><Stack gap={0}>{heads.map((h) => <Code key={h}>{h}</Code>)}</Stack></Row>)}
        {step.for_each && <Row label={t("solutions.panel.forEach")}><Code>{step.for_each.over}</Code> → <Code>{step.for_each.as}</Code>{step.for_each.collect ? <> → <Code>{step.for_each.collect}</Code></> : null}{step.for_each.max ? ` · max ${step.for_each.max}` : ""}</Row>}
        {outcomes.length > 0 && <Row label={t("solutions.outcomes")}><Stack gap={0}>{outcomes.map((o) => <Code key={o}>{o}</Code>)}</Stack></Row>}
      </Table.Tbody>
    </Table>
  );
}

export function StepPanel({
  step,
  element,
  briefing,
  solution,
  busy,
  onOpen,
  onSaveBriefing,
}: {
  step: ElementStep;
  element: SolutionElement;
  briefing: SolutionElement | null;
  solution?: SolutionRow;
  busy: boolean;
  onOpen: (kind: "ai" | "decision" | "tool" | "process", id: string) => void;
  onSaveBriefing?: (fields: BriefingFields) => Promise<void>;
}) {
  const { t } = useI18n();
  const history = element.summary.history?.[step.id];
  return (
    <Stack gap="xs">
      <Group gap="xs" justify="space-between" wrap="nowrap">
        <Group gap="xs" wrap="nowrap">
          <Text fw={700} truncate>{step.id}</Text>
          <Badge size="xs" variant="outline">{t(`workshop.kind.${step.kind}`)}</Badge>
        </Group>
        <Group gap={4}>
          {step.briefing && <Button size="compact-xs" variant="subtle" onClick={() => onOpen("ai", step.briefing!)}>{t("solutions.panel.openFull")}</Button>}
          {step.decision && <Button size="compact-xs" variant="subtle" onClick={() => onOpen("decision", step.decision!)}>{t("solutions.kind.decision")}</Button>}
          {step.tool && <Button size="compact-xs" variant="subtle" onClick={() => onOpen("tool", step.tool!)}>{t("solutions.kind.tool")}</Button>}
          {step.process && <Button size="compact-xs" variant="subtle" onClick={() => onOpen("process", step.process!)}>{t("solutions.kind.process")}</Button>}
        </Group>
      </Group>
      <Divider />
      <Tabs defaultValue="settings" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="settings">{t("solutions.panel.settings")}</Tabs.Tab>
          <Tabs.Tab value="last" rightSection={history?.runs ? <Badge size="xs" circle variant="light">{history.runs}</Badge> : undefined}>{t("solutions.panel.lastRun")}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="settings" pt="xs">
          {step.kind === "agent_step" && step.briefing && briefing ? (
            <BriefingForm element={briefing} solution={solution} onSave={element.writable ? onSaveBriefing : undefined} busy={busy} />
          ) : step.kind === "agent_step" && step.briefing ? (
            <Text size="sm" c="dimmed">…</Text>
          ) : (
            <StepFacts step={step} solution={solution} />
          )}
          {step.kind === "agent_step" && step.briefing && <StepFacts step={step} solution={solution} />}
        </Tabs.Panel>
        <Tabs.Panel value="last" pt="xs">
          <LastRun history={history} stats={element.summary.stats?.[step.id]} />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
