// Werkstatt → Agenten / Werkzeuge: the library of building blocks.
// People think in blocks, not in packages: every agent (briefing) and
// tool of the instance in one list, with where it comes from and what
// uses it. New blocks come from a short form and land in the instance's
// own solution; blocks of installed solutions are read-only and can be
// copied ("adapt a copy").
//
// An agent here is a declared step, not a free actor: it uses reading
// tools itself; a tool with an outside effect runs only in a process
// behind an approval. The page says so where tools are chosen.
import { ActionIcon, Alert, Anchor, Badge, Button, Card, Checkbox, Code, Group, MultiSelect, NumberInput, Select, SimpleGrid, Stack, Text, TextInput, Textarea, Title } from "@mantine/core";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import type { AgentTry, BlockUse, LibraryAgent, LibraryTool, LibraryView, SolutionRow, ToolKind, ToolTry } from "../api/client";
import { useI18n } from "../i18n";
import { useRecordMember } from "./Records";

const message = (err: unknown) => (err instanceof Error ? err.message : String(err));

function useLibrary() {
  const member = useRecordMember();
  const [view, setView] = useState<LibraryView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    if (!member) return;
    member.client.library().then(setView).catch((err: unknown) => setError(message(err)));
  }, [member]);
  useEffect(load, [load]);
  return { member, view, error, setError, load };
}

function Origin({ block }: { block: { own: boolean; solution_name: SolutionRow["name"] } }) {
  const { t, pick } = useI18n();
  return block.own ? <Badge color="teal" variant="light">{t("library.own")}</Badge> : <Badge color="gray" variant="light">{pick(block.solution_name)}</Badge>;
}

function UsedIn({ uses }: { uses: BlockUse[] }) {
  const { t, pick } = useI18n();
  if (!uses.length) return <Text size="sm" c="dimmed">{t("library.unused")}</Text>;
  return (
    <Stack gap={2}>
      {uses.map((u, i) =>
        u.process ? (
          <Anchor key={i} size="sm" component={Link} to={`/workshop/solutions?solution=${encodeURIComponent(u.solution)}&kind=process&id=${encodeURIComponent(u.process)}&node=${encodeURIComponent(u.step ?? "")}`}>
            {pick(u.title) || u.process} · {u.step}
          </Anchor>
        ) : (
          <Anchor key={i} size="sm" component={Link} to={`/workshop/agents?solution=${encodeURIComponent(u.solution)}&id=${encodeURIComponent(u.agent ?? "")}`}>
            {t("library.agent")} {u.agent}
          </Anchor>
        ),
      )}
    </Stack>
  );
}

function Effect({ effect }: { effect: string }) {
  const { t } = useI18n();
  return effect === "read" ? <Badge color="blue" variant="light">{t("library.effect.read")}</Badge> : <Badge color="orange" variant="light">{t("library.effect.external")}</Badge>;
}

const designer = (solution: string, process: string) => `/workshop/solutions?solution=${encodeURIComponent(solution)}&kind=process&id=${encodeURIComponent(process)}`;

// -- processes -------------------------------------------------------------------

export function ProcessesPage() {
  const { t, pick } = useI18n();
  const { view, error } = useLibrary();
  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("nav.processes")}</Title>
        <Group>
          <Button variant="light" component={Link} to="/workshop/agents">{t("library.processFromAgent")}</Button>
          <Button variant="light" component={Link} to="/workshop/solutions">{t("library.processOther")}</Button>
        </Group>
      </Group>
      <Text c="dimmed">{t("library.processesIntro")}</Text>
      {error && <Alert color="red">{error}</Alert>}
      <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
        {(view?.processes ?? []).map((p) => (
          <Card key={`${p.solution}/${p.id}`} withBorder component={Link} to={designer(p.solution, p.id)} style={{ textDecoration: "none", color: "inherit" }}>
            <Stack gap="xs">
              <Group justify="space-between" wrap="nowrap">
                <Text fw={600} truncate>{pick(p.title) || p.id}</Text>
                <Origin block={p} />
              </Group>
              <Group gap="xs">
                {p.sources.map((source) => <Badge key={source} variant="light" color="blue">{t(`library.source.${["manual", "schedule", "folder", "mailbox", "email", "chain"].includes(source) ? source : "other"}`)}</Badge>)}
                <Badge variant="outline" color="gray">{t("library.stepsCount", { n: p.steps })}</Badge>
                {p.agents.length > 0 && <Badge variant="outline" color="gray">{t("library.agentsCount", { n: p.agents.length })}</Badge>}
                {p.tools.length > 0 && <Badge variant="outline" color="gray">{t("library.toolsCount", { n: p.tools.length })}</Badge>}
                <Badge variant="outline" color={p.gates ? "teal" : "gray"}>{t("library.gatesCount", { n: p.gates })}</Badge>
              </Group>
            </Stack>
          </Card>
        ))}
      </SimpleGrid>
      {view && view.processes.length === 0 && <Text c="dimmed">{t("library.noProcesses")}</Text>}
    </Stack>
  );
}

// -- agents ----------------------------------------------------------------------

export function AgentsPage() {
  const { t, pick, lang } = useI18n();
  const { member, view, error, setError, load } = useLibrary();
  const [params, setParams] = useSearchParams();
  const [draft, setDraft] = useState<{ name: string; purpose: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const selected = view?.agents.find((a) => a.solution === params.get("solution") && a.id === params.get("id"));

  const open = (solution: string, id: string) => setParams({ solution, id });
  const create = async () => {
    if (!member || !draft) return;
    setBusy(true);
    setError(null);
    try {
      const made = await member.client.createAgent({ ...draft, lang });
      setDraft(null);
      load();
      open(made.solution, made.id);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  const copy = async (agent: LibraryAgent) => {
    if (!member) return;
    setError(null);
    try {
      const made = await member.client.copyBlock(agent.solution, "ai", agent.id);
      load();
      open(made.solution, made.id);
    } catch (err) {
      setError(message(err));
    }
  };

  if (selected && view && member)
    return <AgentForm key={`${selected.solution}/${selected.id}`} agent={selected} view={view} onBack={() => setParams({})} onSaved={load} onCopy={() => copy(selected)} />;

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("nav.agents")}</Title>
        <Button onClick={() => setDraft({ name: "", purpose: "" })}>{t("library.newAgent")}</Button>
      </Group>
      <Text c="dimmed">{t("library.agentsIntro")}</Text>
      {error && <Alert color="red">{error}</Alert>}
      {draft && (
        <Card withBorder>
          <Stack>
            <TextInput label={t("library.name")} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })} autoFocus />
            <TextInput label={t("library.purpose")} description={t("library.purposeHint")} value={draft.purpose} onChange={(e) => setDraft({ ...draft, purpose: e.currentTarget.value })} />
            <Group>
              <Button loading={busy} disabled={draft.name.trim().length < 2} onClick={create}>{t("library.create")}</Button>
              <Button variant="subtle" onClick={() => setDraft(null)}>{t("library.cancel")}</Button>
            </Group>
          </Stack>
        </Card>
      )}
      <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
        {(view?.agents ?? []).map((a) => (
          <Card key={`${a.solution}/${a.id}`} withBorder style={{ cursor: "pointer" }} onClick={() => open(a.solution, a.id)}>
            <Stack gap="xs">
              <Group justify="space-between" wrap="nowrap">
                <Text fw={600} truncate>{pick(a.name) || a.id}</Text>
                <Origin block={a} />
              </Group>
              <Text size="sm" c="dimmed" lineClamp={2}>{pick(a.description) || "–"}</Text>
              <Group gap="xs">
                {a.knowledge.length > 0 && <Badge variant="outline" color="gray">{t("library.knowledgeCount", { n: a.knowledge.length })}</Badge>}
                {a.tools.length > 0 && <Badge variant="outline" color="gray">{t("library.toolsCount", { n: a.tools.length })}</Badge>}
                <Badge variant="outline" color={a.used_in.length ? "teal" : "gray"}>{t("library.usedCount", { n: a.used_in.length })}</Badge>
              </Group>
            </Stack>
          </Card>
        ))}
      </SimpleGrid>
      {view && view.agents.length === 0 && <Text c="dimmed">{t("library.noAgents")}</Text>}
    </Stack>
  );
}

function AgentForm({ agent, view, onBack, onSaved, onCopy }: { agent: LibraryAgent; view: LibraryView; onBack: () => void; onSaved: () => void; onCopy: () => void }) {
  const { t, pick, lang } = useI18n();
  const member = useRecordMember();
  const [loaded, setLoaded] = useState(false);
  const [name, setName] = useState<Record<string, string>>({});
  const [description, setDescription] = useState<Record<string, string>>({});
  const [role, setRole] = useState<Record<string, string>>({});
  const [instructions, setInstructions] = useState<Record<string, string>>({});
  const [limits, setLimits] = useState("");
  const [knowledge, setKnowledge] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [output, setOutput] = useState<[string, string][]>([]);
  const [modelRole, setModelRole] = useState("");
  const [rest, setRest] = useState<{ flexibility: string; skills: string[]; confidence_threshold: number | null }>({ flexibility: "strict", skills: [], confidence_threshold: null });
  const [roles, setRoles] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const readOnly = !agent.writable;
  const navigate = useNavigate();
  const asProcess = async () => {
    if (!member) return;
    setBusy(true);
    setError(null);
    try {
      const made = await member.client.agentAsProcess(agent.solution, agent.id, lang);
      navigate(designer(made.solution, made.id));
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!member) return;
    Promise.all([member.client.solutionElement(agent.solution, "ai", agent.id), member.client.solutions()])
      .then(([element, solutions]) => {
        const s = element.summary as Record<string, unknown>;
        const table = (v: unknown) => (v && typeof v === "object" ? (v as Record<string, string>) : {});
        setName(table(s.name));
        setDescription(table(s.description));
        setRole(table(s.role));
        setInstructions(table(s.instructions));
        setLimits(((s.limits as Record<string, string>[]) ?? []).map((l) => pick(l)).join("\n"));
        setKnowledge(((s.knowledge as string[]) ?? []).filter((k) => !k.startsWith("{")));
        setTools((s.tools as string[]) ?? []);
        setOutput(Object.entries(table(s.output)));
        setModelRole(String(s.model_role ?? ""));
        setRest({ flexibility: String(s.flexibility ?? "strict"), skills: (s.skills as string[]) ?? [], confidence_threshold: null });
        setRoles(Object.keys(solutions.find((x) => x.id === agent.solution)?.model_roles ?? {}));
        setLoaded(true);
      })
      .catch((err: unknown) => setError(message(err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [member, agent.solution, agent.id]);

  // Tools the agent can see: those of its own solution.
  const toolChoices = useMemo(
    () => view.tools.filter((x) => x.solution === agent.solution).map((x) => ({ value: x.id, label: `${pick(x.name) || x.id} · ${t(x.effect === "read" ? "library.effect.read" : "library.effect.external")}` })),
    [view.tools, agent.solution, pick, t],
  );
  const acting = tools.filter((id) => view.tools.find((x) => x.solution === agent.solution && x.id === id)?.effect !== "read");
  const text = (table: Record<string, string>) => table[lang] ?? pick(table);
  const put = (table: Record<string, string>, value: string) => ({ ...table, [lang]: value });

  const save = async () => {
    if (!member) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await member.client.saveBriefingFields(agent.solution, agent.id, {
        name, description, role, instructions,
        limits: limits.split("\n").map((l) => l.trim()).filter(Boolean),
        model_role: modelRole, flexibility: rest.flexibility, skills: rest.skills, tools, knowledge,
        output: Object.fromEntries(output.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v])),
      });
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <Button variant="subtle" onClick={onBack}>← {t("nav.agents")}</Button>
          <Title order={2}>{text(name) || agent.id}</Title>
          <Origin block={agent} />
        </Group>
        {readOnly ? <Button onClick={onCopy}>{t("library.copy")}</Button> : (
          <Group>
            <Button variant="light" loading={busy} disabled={!loaded} onClick={asProcess} title={t("library.asProcessHint")}>{t("library.asProcess")}</Button>
            <Button loading={busy} disabled={!loaded} onClick={save}>{t("library.save")}</Button>
          </Group>
        )}
      </Group>
      {readOnly && <Alert color="gray">{t("library.readOnly")}</Alert>}
      {error && <Alert color="red" style={{ whiteSpace: "pre-wrap" }}>{error}</Alert>}
      {saved && <Alert color="teal">{t("library.saved")}</Alert>}
      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Stack>
          <Card withBorder>
            <Stack>
              <TextInput label={t("library.name")} value={text(name)} onChange={(e) => setName(put(name, e.currentTarget.value))} readOnly={readOnly} />
              <TextInput label={t("library.purpose")} value={text(description)} onChange={(e) => setDescription(put(description, e.currentTarget.value))} readOnly={readOnly} />
              <Textarea label={t("library.role")} description={t("library.roleHint")} autosize minRows={2} value={text(role)} onChange={(e) => setRole(put(role, e.currentTarget.value))} readOnly={readOnly} />
              <Textarea label={t("library.instructions")} description={t("library.instructionsHint")} autosize minRows={6} value={text(instructions)} onChange={(e) => setInstructions(put(instructions, e.currentTarget.value))} readOnly={readOnly} />
              <Textarea label={t("library.limits")} description={t("library.limitsHint")} autosize minRows={2} value={limits} onChange={(e) => setLimits(e.currentTarget.value)} readOnly={readOnly} />
            </Stack>
          </Card>
          <Card withBorder>
            <Stack>
              <Title order={4}>{t("library.result")}</Title>
              <Text size="sm" c="dimmed">{t("library.resultHint")}</Text>
              {output.map(([k, v], i) => (
                <Group key={i} wrap="nowrap" align="flex-end">
                  <TextInput w={180} label={i === 0 ? t("library.field") : undefined} value={k} onChange={(e) => setOutput(output.map((row, j) => (j === i ? [e.currentTarget.value, row[1]] : row)))} readOnly={readOnly} />
                  <TextInput style={{ flex: 1 }} label={i === 0 ? t("library.fieldMeaning") : undefined} value={v} onChange={(e) => setOutput(output.map((row, j) => (j === i ? [row[0], e.currentTarget.value] : row)))} readOnly={readOnly} />
                  {!readOnly && <ActionIcon variant="subtle" color="gray" aria-label={t("library.remove")} onClick={() => setOutput(output.filter((_, j) => j !== i))}>✕</ActionIcon>}
                </Group>
              ))}
              {!readOnly && <Button variant="light" w="fit-content" onClick={() => setOutput([...output, ["", ""]])}>{t("library.addField")}</Button>}
            </Stack>
          </Card>
        </Stack>
        <Stack>
          <Card withBorder>
            <Stack>
              <MultiSelect label={t("library.knowledge")} description={t("library.knowledgeHint")} data={view.knowledge.map((k) => ({ value: k.id, label: pick(k.title) || k.id }))} value={knowledge} onChange={setKnowledge} readOnly={readOnly} searchable />
              <MultiSelect label={t("nav.tools")} description={t("library.toolsHint")} data={toolChoices} value={tools} onChange={setTools} readOnly={readOnly} searchable />
              {acting.length > 0 && <Alert color="orange">{t("library.actingHint")}</Alert>}
              <Select label={t("library.modelRole")} description={t("library.modelRoleHint")} data={roles} value={modelRole || null} onChange={(v) => setModelRole(v ?? "")} readOnly={readOnly} />
            </Stack>
          </Card>
          <TryAgent agent={agent} />
          <Card withBorder>
            <Stack gap="xs">
              <Title order={4}>{t("library.usedIn")}</Title>
              <UsedIn uses={agent.used_in} />
            </Stack>
          </Card>
        </Stack>
      </SimpleGrid>
    </Stack>
  );
}

const show = (value: unknown) => (typeof value === "string" ? value : JSON.stringify(value, null, 2));

/** Run the agent once on an example - the saved version, a real model
 * call under every rule of a step, without a case - and keep what was
 * tried as a proving case. */
function TryAgent({ agent }: { agent: LibraryAgent }) {
  const { t } = useI18n();
  const member = useRecordMember();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<AgentTry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [caseId, setCaseId] = useState("");
  const [assert, setAssert] = useState<string[]>([]);
  const [kept, setKept] = useState<string | null>(null);

  const run = async () => {
    if (!member) return;
    setBusy(true);
    setError(null);
    setReport(null);
    setKept(null);
    try {
      const result = await member.client.tryAgent(agent.solution, agent.id, text);
      setReport(result);
      setAssert([]);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  const keep = async () => {
    if (!member || !report?.result) return;
    setBusy(true);
    setError(null);
    try {
      const fields = Object.fromEntries(assert.map((k) => [k, report.result?.[k]]));
      const result = await member.client.keepAgentCase(agent.solution, agent.id, { id: caseId, text, fields });
      setKept(result.record_error ? t("library.try.keptUnrecorded", { reason: result.record_error }) : t(result.run?.ok === false ? "library.try.keptRed" : "library.try.kept", { id: result.id }));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card withBorder>
      <Stack gap="sm">
        <Title order={4}>{t("library.try.title")}</Title>
        <Text size="sm" c="dimmed">{t("library.try.agentHint")}</Text>
        <Textarea autosize minRows={3} maxRows={12} placeholder={t("library.try.placeholder")} value={text} onChange={(e) => setText(e.currentTarget.value)} />
        <Button w="fit-content" variant="light" loading={busy} disabled={!text.trim()} onClick={run}>{t("library.try.run")}</Button>
        {error && <Alert color="red" style={{ whiteSpace: "pre-wrap" }}>{error}</Alert>}
        {report && !report.ok && <Alert color="red" title={t("library.try.failed")}>{report.error}</Alert>}
        {report?.ok && report.result && (
          <Stack gap="xs">
            {(report.flags ?? []).map((flag, i) => <Alert key={i} color="orange">{flag}</Alert>)}
            {Object.entries(report.result).map(([key, value]) => (
              <div key={key}>
                <Group gap="xs">
                  {agent.writable && agent.outputs.includes(key) && (
                    <Checkbox size="xs" aria-label={t("library.try.assert", { field: key })} checked={assert.includes(key)} onChange={(e) => setAssert(e.currentTarget.checked ? [...assert, key] : assert.filter((k) => k !== key))} />
                  )}
                  <Text size="sm" fw={600}>{key}</Text>
                </Group>
                <Code block style={{ whiteSpace: "pre-wrap" }}>{show(value)}</Code>
              </div>
            ))}
            <Text size="xs" c="dimmed">
              {[report.model, report.ms != null ? `${report.ms} ms` : null, report.cost_chf != null ? `CHF ${report.cost_chf.toFixed(4)}` : null, (report.knowledge_sources?.length ?? 0) > 0 ? t("library.knowledgeCount", { n: report.knowledge_sources?.length ?? 0 }) : null].filter(Boolean).join(" · ")}
            </Text>
            {agent.writable && (
              <Group align="flex-end">
                <TextInput label={t("library.try.caseName")} description={t("library.try.caseHint")} placeholder="offerte-ohne-preis" value={caseId} onChange={(e) => setCaseId(e.currentTarget.value.toLowerCase().replace(/[^a-z0-9-]+/g, "-"))} style={{ flex: 1 }} />
                <Button variant="light" loading={busy} disabled={!/^[a-z][a-z0-9-]{1,59}$/.test(caseId)} onClick={keep}>{t("library.try.keep")}</Button>
              </Group>
            )}
            {kept && <Alert color="teal">{kept}</Alert>}
          </Stack>
        )}
      </Stack>
    </Card>
  );
}

/** Run one tool on given inputs: a reading tool for real, a tool that
 * acts outside only as a preview of the request - nothing is sent. */
function TryTool({ tool }: { tool: LibraryTool }) {
  const { t } = useI18n();
  const member = useRecordMember();
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<ToolTry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    if (!member) return;
    setBusy(true);
    setError(null);
    try {
      setReport(await member.client.tryTool(tool.solution, tool.id, Object.fromEntries(Object.keys(tool.inputs).map((k) => [k, inputs[k] ?? ""]))));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="xs">
      {Object.entries(tool.inputs).map(([key, hint]) => (
        <TextInput key={key} size="xs" label={key} description={hint !== key ? hint : undefined} value={inputs[key] ?? ""} onChange={(e) => setInputs({ ...inputs, [key]: e.currentTarget.value })} />
      ))}
      <Button size="xs" w="fit-content" variant="light" loading={busy} onClick={run}>{t(tool.effect === "read" ? "library.try.run" : "library.try.preview")}</Button>
      {error && <Alert color="red">{error}</Alert>}
      {report && !report.ok && <Alert color="red">{report.error}</Alert>}
      {report?.ok && report.mode === "live" && (
        <>
          {(report.flags ?? []).map((flag, i) => <Alert key={i} color="orange">{flag}</Alert>)}
          <Code block style={{ whiteSpace: "pre-wrap", maxHeight: 260, overflow: "auto" }}>{show(report.outputs)}</Code>
          <Text size="xs" c="dimmed">{report.ms} ms</Text>
        </>
      )}
      {report?.ok && report.mode === "preview" && (
        <>
          <Alert color="orange">{t("library.try.notSent")}</Alert>
          <Code block style={{ whiteSpace: "pre-wrap" }}>{show(report.request)}</Code>
        </>
      )}
    </Stack>
  );
}

// -- tools -----------------------------------------------------------------------

interface ToolDraft {
  kind: ToolKind;
  name: string;
  description: string;
  url: string;
  method: string;
  params: string;
  response: string;
  provider: string;
  secret: string;
  max: number;
}

const newDraft = (kind: ToolKind): ToolDraft => ({ kind, name: "", description: "", url: "https://", method: "POST", params: "", response: "", provider: "duckduckgo", secret: "", max: 5 });

/** "name = path" lines -> {name: path}. */
function mapping(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const at = line.indexOf("=");
    const key = (at < 0 ? line : line.slice(0, at)).trim();
    if (key) out[key] = at < 0 ? "" : line.slice(at + 1).trim();
  }
  return out;
}

export function ToolsPage() {
  const { t, pick, lang } = useI18n();
  const { member, view, error, setError, load } = useLibrary();
  const [draft, setDraft] = useState<ToolDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<string | null>(null);
  const [trying, setTrying] = useState<string | null>(null);

  const create = async () => {
    if (!member || !draft) return;
    setBusy(true);
    setError(null);
    setMade(null);
    const list = draft.params.split(/[\s,]+/).filter(Boolean);
    const form: Record<string, unknown> =
      draft.kind === "search" ? { provider: draft.provider, secret: draft.secret, max: draft.max }
      : draft.kind === "json" ? { url: draft.url, params: list, response: mapping(draft.response) }
      : draft.kind === "api" ? { url: draft.url, method: draft.method, params: list, secret: draft.secret, response: mapping(draft.response) }
      : {};
    try {
      const result = await member.client.createTool({ kind: draft.kind, name: draft.name, description: draft.description, lang, form });
      setDraft(null);
      setMade(result.id);
      load();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  const copy = async (tool: LibraryTool) => {
    if (!member) return;
    setError(null);
    try {
      const result = await member.client.copyBlock(tool.solution, "tool", tool.id);
      setMade(result.id);
      load();
    } catch (err) {
      setError(message(err));
    }
  };

  const tiles: ToolKind[] = ["page", "search", "json", "api", "mcp", "client"];
  return (
    <Stack>
      <Title order={2}>{t("nav.tools")}</Title>
      <Text c="dimmed">{t("library.toolsIntro")}</Text>
      {error && <Alert color="red" style={{ whiteSpace: "pre-wrap" }}>{error}</Alert>}
      {made && <Alert color="teal">{t("library.toolMade", { id: made })}</Alert>}
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
        {tiles.map((kind) => (
          <Card key={kind} withBorder>
            <Stack gap="xs">
              <Group justify="space-between" wrap="nowrap">
                <Text fw={600}>{t(`library.kind.${kind}.title`)}</Text>
                {view?.form_kinds.includes(kind) ? (
                  <Button size="xs" variant="light" onClick={() => setDraft(newDraft(kind))}>{t("library.new")}</Button>
                ) : kind === "mcp" ? (
                  <Button size="xs" variant="light" component={Link} to="/settings/mcp">{t("library.new")}</Button>
                ) : null}
              </Group>
              <Text size="sm" c="dimmed">{t(`library.kind.${kind}.text`)}</Text>
              <Effect effect={kind === "page" || kind === "search" || kind === "json" ? "read" : "external"} />
            </Stack>
          </Card>
        ))}
      </SimpleGrid>
      {draft && (
        <Card withBorder>
          <Stack>
            <Title order={4}>{t(`library.kind.${draft.kind}.title`)}</Title>
            <TextInput label={t("library.name")} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })} autoFocus />
            <TextInput label={t("library.toolDescription")} description={t("library.toolDescriptionHint")} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.currentTarget.value })} />
            {draft.kind === "search" && (
              <Group align="flex-end">
                <Select label={t("library.provider")} data={["duckduckgo", "brave", "tavily"]} value={draft.provider} onChange={(v) => setDraft({ ...draft, provider: v ?? "duckduckgo" })} allowDeselect={false} />
                {draft.provider !== "duckduckgo" && <TextInput label={t("library.secret")} description={t("library.secretHint")} value={draft.secret} onChange={(e) => setDraft({ ...draft, secret: e.currentTarget.value })} />}
                <NumberInput label={t("library.maxHits")} min={1} max={10} value={draft.max} onChange={(v) => setDraft({ ...draft, max: Number(v) || 5 })} w={120} />
              </Group>
            )}
            {(draft.kind === "json" || draft.kind === "api") && (
              <>
                <Group align="flex-end" wrap="nowrap">
                  {draft.kind === "api" && <Select label={t("library.method")} w={120} data={["GET", "POST", "PUT", "PATCH", "DELETE"]} value={draft.method} onChange={(v) => setDraft({ ...draft, method: v ?? "POST" })} allowDeselect={false} />}
                  <TextInput style={{ flex: 1 }} label={t("library.url")} description={t(draft.kind === "api" ? "library.urlHintApi" : "library.urlHintJson")} value={draft.url} onChange={(e) => setDraft({ ...draft, url: e.currentTarget.value })} />
                </Group>
                <TextInput label={t("library.params")} description={t("library.paramsHint")} value={draft.params} onChange={(e) => setDraft({ ...draft, params: e.currentTarget.value })} />
                <Textarea label={t("library.response")} description={t("library.responseHint")} autosize minRows={2} value={draft.response} onChange={(e) => setDraft({ ...draft, response: e.currentTarget.value })} />
                {draft.kind === "api" && <TextInput label={t("library.secret")} description={t("library.secretHint")} value={draft.secret} onChange={(e) => setDraft({ ...draft, secret: e.currentTarget.value })} />}
              </>
            )}
            <Group>
              <Button loading={busy} disabled={draft.name.trim().length < 2} onClick={create}>{t("library.create")}</Button>
              <Button variant="subtle" onClick={() => setDraft(null)}>{t("library.cancel")}</Button>
            </Group>
          </Stack>
        </Card>
      )}
      <Title order={4}>{t("library.allTools")}</Title>
      <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
        {(view?.tools ?? []).map((tool) => (
          <Card key={`${tool.solution}/${tool.id}`} withBorder>
            <Stack gap="xs">
              <Group justify="space-between" wrap="nowrap">
                <Text fw={600} truncate>{pick(tool.name) || tool.id}</Text>
                <Origin block={tool} />
              </Group>
              <Text size="sm" c="dimmed" lineClamp={2}>{pick(tool.description)}</Text>
              <Group gap="xs">
                <Badge variant="outline" color="gray">{t(`library.kind.${tool.kind}.title`)}</Badge>
                <Effect effect={tool.effect} />
              </Group>
              <UsedIn uses={tool.used_in} />
              <Group gap="xs">
                <Anchor size="sm" component={Link} to={`/workshop/solutions?solution=${encodeURIComponent(tool.solution)}&kind=tool&id=${encodeURIComponent(tool.id)}`}>{t("library.openSource")}</Anchor>
                {!tool.own && <Anchor size="sm" component="button" onClick={() => copy(tool)}>{t("library.copy")}</Anchor>}
                {tool.kind !== "action" && tool.kind !== "client" && (
                  <Anchor size="sm" component="button" onClick={() => setTrying(trying === `${tool.solution}/${tool.id}` ? null : `${tool.solution}/${tool.id}`)}>{t("library.try.title")}</Anchor>
                )}
              </Group>
              {trying === `${tool.solution}/${tool.id}` && <TryTool tool={tool} />}
            </Stack>
          </Card>
        ))}
      </SimpleGrid>
      {view && view.tools.length === 0 && <Text c="dimmed">{t("library.noTools")}</Text>}
    </Stack>
  );
}
