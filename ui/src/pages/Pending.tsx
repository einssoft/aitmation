// Pendenzen: every waiting HumanGate across the household. The gate view
// is rendered from its declaration (ADR-0021 §7): generic outcomes,
// or the two special gates the core knows (categorization, addressing).
import { Alert, Badge, Button, Card, Group, MultiSelect, Select, Stack, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, type ClientTask, type Gate, type Member } from "../api/client";
import { ResultsView } from "../components/ResultsView";
import { TaskCard } from "../components/TaskCard";
import { useAcrossHousehold, useHousehold } from "../household";
import { LANGUAGE_NAMES, useI18n, type Language } from "../i18n";
import { DocumentView } from "../components/DocumentView";
import { UploadBox } from "../components/UploadBox";

export function GateCard({ gate, onDone }: { gate: Gate & { _member: Member }; onDone: () => void }) {
  const { t, pick, formatDate, lang } = useI18n();
  const { members } = useHousehold();
  const client = members.find((m) => m.member.id === gate._member.id)?.client;
  const [comment, setComment] = useState("");
  const [choice, setChoice] = useState<string | null>(typeof gate.suggested === "string" ? gate.suggested : null);
  const [several, setSeveral] = useState<string[]>(Array.isArray(gate.suggested) ? gate.suggested : []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async (body: Record<string, unknown>) => {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      await client.decide(gate.case_id, gate.step_id, { ...body, comment, actor: "ui" });
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const options = gate.options.map((o) => ({ value: o.id, label: o.titles ? pick(o.titles) : o.title }));
  const outcomeLabel = (o: string) => {
    const key = `pending.outcome.${o}`;
    const text = t(key);
    return text === key ? o : text;
  };

  return (
    <Card withBorder padding="md">
      <Group justify="space-between" align="flex-start">
        <div>
          <Group gap="xs">
            <Text fw={600}>{pick(gate.template_titles) || gate.template_title}</Text>
            <Badge variant="light">{gate._member.title}</Badge>
            {gate.app && <Badge variant="outline" color="gray">{gate.app}</Badge>}
            {gate.notified?.escalation && <Badge variant="filled" color="red" size="sm">{t("pending.escalated")}</Badge>}
            {!gate.notified?.escalation && gate.notified?.reminder && <Badge variant="light" color="orange" size="sm">{t("pending.reminded")}</Badge>}
          </Group>
          <Text size="sm" c="dimmed">
            {t("pending.step")} <code>{gate.step_id}</code> · {t("pending.waitingSince", { when: formatDate(gate.started_at) })}
            {gate.deadline_at && ` · ${t("pending.deadline", { when: formatDate(gate.deadline_at) })}`}
          </Text>
          {gate.subject && (
            <Text size="sm" mt={4} fw={500}>
              {gate.subject}
              {gate.note && <Text span c="dimmed" fw={400}> — {gate.note}</Text>}
            </Text>
          )}
          {gate.document && (gate.document.original_name || gate.document.subject || gate.document.source) && (
            <Text size="sm" mt={4}>
              {t("pending.document")}: {gate.document.original_name ?? gate.document.subject ?? "–"}
              {gate.document.from && ` · ${gate.document.from}`}
              {gate.document.source === "web" && ` · ${t("pending.viaWeb")}`}
              {typeof gate.document.language === "string" && (
                <Badge ml="xs" size="xs" variant="outline">{LANGUAGE_NAMES[gate.document.language as Language] ?? gate.document.language}</Badge>
              )}
            </Text>
          )}
          {client && gate.document && (
            <DocumentView client={client} caseId={gate.case_id} hasDocument={gate.document.has_file === true} name={typeof gate.document.original_name === "string" ? gate.document.original_name : null} />
          )}
          {client && <ResultsView client={client} caseId={gate.case_id} results={gate.results} media={gate.media} stubSteps={gate.stub_steps} />}
        </div>
        <Button component={Link} to={`/cases/${gate._member.id}/${gate.case_id}`} variant="subtle" size="xs">
          {t("pending.case")}
        </Button>
      </Group>

      <Stack gap="xs" mt="md">
        {gate.gate_kind === "categorization" && (
          <Select
            label={t("pending.chooseCategory")}
            data={options}
            value={choice}
            onChange={setChoice}
            placeholder={options.length ? undefined : t("pending.noOptions")}
            disabled={!options.length}
            searchable
          />
        )}
        {gate.gate_kind === "addressing" && (
          <MultiSelect
            label={t("pending.chooseAddressee")}
            description={t("pending.several")}
            data={options}
            value={several.length ? several : choice ? [choice] : []}
            onChange={setSeveral}
            placeholder={options.length ? undefined : t("pending.noOptions")}
            disabled={!options.length}
            searchable
          />
        )}
        <TextInput label={t("pending.comment")} value={comment} onChange={(e) => setComment(e.currentTarget.value)} size="xs" />
        {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
        <Group gap="xs">
          {gate.gate_kind === "categorization" && (
            <>
              <Button size="xs" disabled={!choice} loading={busy} onClick={() => send({ category: choice })}>
                {t("pending.assign")}
              </Button>
              <Button size="xs" variant="default" loading={busy} onClick={() => send({ discard: true })}>
                {t("pending.discard")}
              </Button>
            </>
          )}
          {gate.gate_kind === "addressing" && (
            <>
              <Button
                size="xs"
                disabled={!several.length && !choice}
                loading={busy}
                onClick={() => send({ addressees: several.length ? several : [choice] })}
              >
                {t("pending.deliver")}
              </Button>
              <Button size="xs" variant="default" loading={busy} onClick={() => send({ discard: true })}>
                {t("pending.discard")}
              </Button>
            </>
          )}
          {gate.gate_kind === "generic" &&
            gate.outcomes.map((o) => (
              <Button key={o} size="xs" variant={o === "approved" ? "filled" : "default"} loading={busy} onClick={() => send({ outcome: o })}>
                {outcomeLabel(o)}
              </Button>
            ))}
        </Group>
      </Stack>
      <span hidden>{lang}</span>
    </Card>
  );
}

export function PendingPage() {
  const { t } = useI18n();
  const { rows, loading, error, refresh } = useAcrossHousehold<Gate>((c) => c.gates());
  const tasks = useAcrossHousehold<ClientTask>((c) => c.clientTasks());
  const [showUpload, setShowUpload] = useState(false);
  const refreshAll = () => { refresh(); tasks.refresh(); };
  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("pending.title")}</Title>
        <Button variant={showUpload ? "light" : "default"} size="xs" onClick={() => setShowUpload((v) => !v)}>
          {t("upload.title")}
        </Button>
      </Group>
      {showUpload && <UploadBox onDone={() => setTimeout(refreshAll, 2500)} />}
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {loading && !rows.length && <Text c="dimmed">{t("app.loading")}</Text>}
      {!loading && !rows.length && !tasks.rows.length && <Text c="dimmed">{t("pending.empty")}</Text>}
      {tasks.rows.map((task) => (
        <TaskCard key={`${task._member.id}:${task.id}`} task={task} onDone={() => setTimeout(refreshAll, 800)} />
      ))}
      {rows.map((g) => (
        <GateCard key={`${g._member.id}:${g.case_id}:${g.step_id}`} gate={g} onDone={refreshAll} />
      ))}
    </Stack>
  );
}
