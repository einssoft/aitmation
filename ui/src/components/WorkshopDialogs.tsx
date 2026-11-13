// Werkstatt → Lösungen: the "new" dialogs (ADR-0035). Each one asks for the
// few facts a scaffold needs; the core writes a complete, validated element
// and reloads. Nothing here composes files - the API does.
import { Button, Checkbox, FileInput, Group, Modal, Select, Stack, Text, TextInput, Textarea } from "@mantine/core";
import { useI18n as useI18nForStart } from "../i18n";
import { useEffect, useState } from "react";
import { type ElementStep, type NewCaseBody, type NewElementBody, type SolutionRow } from "../api/client";
import { useI18n } from "../i18n";

const ID = /^[a-z0-9][a-z0-9_-]*$/;

function slug(text: string): string {
  return text
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

export function NewSolutionDialog({ opened, busy, onClose, onCreate }: {
  opened: boolean;
  busy: boolean;
  onClose: () => void;
  onCreate: (body: { id: string; name: string; description: string }) => Promise<void>;
}) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const [touched, setTouched] = useState(false);
  const [description, setDescription] = useState("");
  useEffect(() => { if (opened) { setName(""); setId(""); setTouched(false); setDescription(""); } }, [opened]);
  const valid = ID.test(id) && name.trim().length > 0;
  return (
    <Modal opened={opened} onClose={onClose} title={t("solutions.new.solution")}>
      <Stack>
        <TextInput label={t("solutions.new.name")} value={name} required onChange={(e) => { setName(e.currentTarget.value); if (!touched) setId(slug(e.currentTarget.value)); }} />
        <TextInput label={t("solutions.new.id")} description={t("solutions.new.idHelp")} value={id} required error={id && !ID.test(id) ? t("solutions.new.idInvalid") : undefined} onChange={(e) => { setTouched(true); setId(e.currentTarget.value); }} />
        <Textarea label={t("solutions.new.description")} value={description} autosize minRows={2} onChange={(e) => setDescription(e.currentTarget.value)} />
        <Group justify="flex-end">
          <Button variant="subtle" onClick={onClose}>{t("solutions.cancel")}</Button>
          <Button loading={busy} disabled={!valid} onClick={() => onCreate({ id, name: name.trim(), description: description.trim() })}>{t("solutions.new.create")}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function NewElementDialog({ kind, solution, templates = [], busy, onClose, onCreate }: {
  kind: "process" | "ai" | "decision" | null;
  solution: SolutionRow | null;
  /** processes of all installed solutions, as "solution/process" */
  templates?: { value: string; label: string }[];
  busy: boolean;
  onClose: () => void;
  onCreate: (kind: "process" | "ai" | "decision", body: NewElementBody) => Promise<void>;
}) {
  const { t } = useI18n();
  const [title, setTitle] = useState("");
  const [id, setId] = useState("");
  const [touched, setTouched] = useState(false);
  const [ai, setAi] = useState("");
  const [modelRole, setModelRole] = useState("default");
  const [source, setSource] = useState<NonNullable<NewElementBody["source"]>>("manual");
  const [template, setTemplate] = useState("");
  useEffect(() => {
    if (kind) { setTitle(""); setId(""); setTouched(false); setAi(""); setSource("manual"); setTemplate(""); setModelRole(Object.keys(solution?.model_roles ?? {})[0] ?? "default"); }
  }, [kind, solution]);
  if (!kind || !solution) return null;
  const roles = Object.keys(solution.model_roles);
  const briefings = solution.tree.ai;
  const valid = ID.test(id) && (!ai || ID.test(ai));
  return (
    <Modal opened onClose={onClose} title={t(`solutions.new.${kind}`)}>
      <Stack>
        <TextInput label={t("solutions.new.title")} value={title} onChange={(e) => { setTitle(e.currentTarget.value); if (!touched) setId(slug(e.currentTarget.value)); }} />
        <TextInput label={t("solutions.new.id")} description={t("solutions.new.idHelp")} value={id} required error={id && !ID.test(id) ? t("solutions.new.idInvalid") : undefined} onChange={(e) => { setTouched(true); setId(e.currentTarget.value); }} />
        {kind === "process" && templates.length > 0 && (
          <Select label={t("solutions.new.template")} description={t("solutions.new.templateHelp")} searchable clearable
            data={[{ value: "", label: t("solutions.new.templateNone") }, ...templates]} value={template} onChange={(v) => setTemplate(v ?? "")} />
        )}
        {kind === "process" && !template && (
          <>
            <TextInput label={t("solutions.new.ai")} description={t("solutions.new.aiHelp", { existing: briefings.join(", ") || "–" })} placeholder={id} value={ai} onChange={(e) => setAi(e.currentTarget.value)} />
            <Select label={t("solutions.new.source")} data={["manual", "chain", "folder", "mailbox", "schedule"].map((v) => ({ value: v, label: t(`solutions.new.source_${v}`) }))} value={source} onChange={(v) => v && setSource(v as typeof source)} />
          </>
        )}
        {(kind === "process" || kind === "ai") && roles.length > 1 && (
          <Select label={t("solutions.new.modelRole")} data={roles} value={modelRole} onChange={(v) => v && setModelRole(v)} />
        )}
        <Text size="xs" c="dimmed">{t(kind === "process" ? "solutions.new.processHelp" : kind === "ai" ? "solutions.new.aiKindHelp" : "solutions.new.decisionKindHelp")}</Text>
        <Group justify="flex-end">
          <Button variant="subtle" onClick={onClose}>{t("solutions.cancel")}</Button>
          <Button loading={busy} disabled={!valid} onClick={() => onCreate(kind, { id, title: title.trim() || undefined, ai: ai || undefined, model_role: modelRole, source, template: template || undefined })}>{t("solutions.new.create")}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function NewCaseDialog({ opened, steps, samples, busy, canRecord, onClose, onCreate }: {
  opened: boolean;
  steps: ElementStep[];
  samples: string[];
  busy: boolean;
  canRecord: boolean;
  onClose: () => void;
  onCreate: (body: NewCaseBody) => Promise<void>;
}) {
  const { t } = useI18n();
  const [id, setId] = useState("");
  const [sample, setSample] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<"completed" | "running">("completed");
  const [run, setRun] = useState(true);
  const [record, setRecord] = useState(false);
  useEffect(() => { if (opened) { setId(""); setSample(samples[0] ?? null); setDescription(""); setDecisions({}); setStatus("completed"); setRun(true); setRecord(false); } }, [opened, samples]);
  const gates = steps.filter((s) => s.kind === "human_gate");
  const valid = ID.test(id) && !!sample;
  return (
    <Modal opened={opened} onClose={onClose} title={t("solutions.new.case")}>
      <Stack>
        {!samples.length && <Text size="sm" c="orange">{t("solutions.new.noSamples")}</Text>}
        <Select label={t("solutions.new.sample")} data={samples} value={sample} onChange={setSample} searchable required />
        <TextInput label={t("solutions.new.id")} description={t("solutions.new.caseIdHelp")} value={id} required error={id && !ID.test(id) ? t("solutions.new.idInvalid") : undefined} onChange={(e) => setId(e.currentTarget.value)} />
        <TextInput label={t("solutions.new.description")} value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        {gates.map((g) => (
          <Select
            key={g.id}
            label={t("solutions.new.decisionAt", { step: g.id })}
            description={t("solutions.new.decisionHelp")}
            data={g.outcomes}
            value={decisions[g.id] ?? null}
            clearable
            onChange={(v) => setDecisions((d) => { const next = { ...d }; if (v) next[g.id] = v; else delete next[g.id]; return next; })}
          />
        ))}
        <Select label={t("solutions.new.status")} data={[{ value: "completed", label: t("solutions.new.status_completed") }, { value: "running", label: t("solutions.new.status_running") }]} value={status} onChange={(v) => v && setStatus(v as typeof status)} />
        <Checkbox label={t("solutions.new.runNow")} checked={run} onChange={(e) => setRun(e.currentTarget.checked)} />
        {run && canRecord && <Checkbox label={t("solutions.new.record")} description={t("solutions.new.recordHelp")} checked={record} onChange={(e) => setRecord(e.currentTarget.checked)} />}
        <Group justify="flex-end">
          <Button variant="subtle" onClick={onClose}>{t("solutions.cancel")}</Button>
          <Button loading={busy} disabled={!valid} onClick={() => onCreate({ id, sample: sample!, description: description.trim() || undefined, decisions, status, run, record: run && record })}>{t("solutions.new.create")}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}


/** Start a manual process by hand: one field per contract input. A value
 *  that looks like a number or a JSON list is sent as such. */
export function StartDialog({ opened, processId, contract, initial, busy, onClose, onStart }: {
  opened: boolean;
  processId: string;
  contract: Record<string, string>;
  initial?: Record<string, string>;
  busy: boolean;
  onClose: () => void;
  onStart: (payload: Record<string, unknown>, file?: { name: string; content_base64: string }) => Promise<void>;
}) {
  const { t } = useI18nForStart();
  const [values, setValues] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);
  useEffect(() => { if (opened) { setValues(initial ?? {}); setFile(null); } }, [opened, initial]);
  const toBase64 = (f: File) => new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(f);
  });
  const payload = () => Object.fromEntries(
    Object.entries(values).filter(([, v]) => v.trim() !== "").map(([k, v]) => {
      const text = v.trim();
      if (/^-?\d+(\.\d+)?$/.test(text)) return [k, Number(text)];
      if (text.startsWith("[") || text.startsWith("{")) { try { return [k, JSON.parse(text)]; } catch { return [k, text]; } }
      return [k, text];
    }),
  );
  const names = Object.keys(contract);
  return (
    <Modal opened={opened} onClose={onClose} title={t("solutions.start.title", { id: processId })}>
      <Stack>
        <Text size="xs" c="dimmed">{t("solutions.start.help")}</Text>
        {names.length === 0 && <Text size="sm" c="dimmed">{t("solutions.start.noInputs")}</Text>}
        {names.map((name) => name === "file" ? (
          <FileInput key={name} label={name} description={contract[name]} value={file} onChange={setFile} />
        ) : (
          <TextInput key={name} label={name} description={contract[name]} value={values[name] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [name]: e.currentTarget.value }))} />
        ))}
        <TextInput label={t("solutions.start.extra")} description={t("solutions.start.extraHelp")} placeholder='platforms=["instagram","tiktok"]' onChange={(e) => {
          const [k, ...rest] = e.currentTarget.value.split("=");
          if (k && rest.length) setValues((v) => ({ ...v, [k.trim()]: rest.join("=") }));
        }} />
        <Group justify="flex-end">
          <Button variant="subtle" onClick={onClose}>{t("solutions.cancel")}</Button>
          <Button loading={busy} disabled={names.includes("file") && !file} onClick={async () => onStart(payload(), file ? { name: file.name, content_base64: await toBase64(file) } : undefined)}>{t("solutions.start.run")}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
