// Chat: an input path into the instance's own views, not a chatbot
// (ZIELBILD §9, ADR-0021). A question is answered by the core with a key
// plus data; open decisions arrive as full gates and are decided here with
// the same controls as in Pendenzen — the gate stays the gate (ADR-0007).
import { Alert, Anchor, Badge, Button, Card, Group, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError, type CaseRow, type ChatAnswer, type ChatStart, type Gate, type Member } from "../api/client";
import { useHousehold } from "../household";
import { useI18n } from "../i18n";
import { GateCard } from "./Pending";
import { UploadBox } from "../components/UploadBox";
import { StartDialog } from "../components/WorkshopDialogs";

interface Turn {
  id: number;
  question: string;
  answers: { member: Member; answer: ChatAnswer }[];
  error?: string;
}

/** With several mandants, "nothing here" answers are noise as soon as one
 * member has something to show — keep them only when all are empty. */
function visible(answers: Turn["answers"]): Turn["answers"] {
  const hasData = (a: ChatAnswer) => Boolean(a.gates?.length || a.cases?.length || a.case || a.start || a.start_options?.length);
  const withData = answers.filter((x) => hasData(x.answer));
  if (!withData.length) return answers.slice(0, 1);
  return answers.filter((x) => hasData(x.answer) || x.answer.intent === "unknown" || x.answer.intent === "help");
}

export function ChatPage() {
  const { t, pick, formatDate } = useI18n();
  const { scope } = useHousehold();
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  // "Erstelle einen Post über …": the chat brings the start form, the person
  // confirms - nothing starts from a sentence alone.
  const [starting, setStarting] = useState<{ member: Member; start: ChatStart } | null>(null);
  const [startBusy, setStartBusy] = useState(false);
  const counter = useRef(0);

  const startProcess = async (payload: Record<string, unknown>, file?: { name: string; content_base64: string }) => {
    if (!starting) return;
    const client = scope.find((m) => m.member.id === starting.member.id)?.client;
    if (!client) return;
    setStartBusy(true);
    try {
      const { case_id } = await client.startTemplate(starting.start.process_id, payload, file);
      const detail = await client.case(case_id);
      const id = ++counter.current;
      const question = pick(starting.start.titles) || starting.start.process_id;
      const answer: ChatAnswer = { intent: "case", answer: "chat.answer.started", params: { id: case_id.slice(0, 8) }, case: detail };
      setTurns((prev) => [...prev, { id, question, answers: [{ member: starting.member, answer }] }]);
      setStarting(null);
    } catch (err) {
      setTurns((prev) => [...prev, { id: ++counter.current, question: "", answers: [], error: err instanceof ApiError ? err.message : String(err) }]);
    } finally {
      setStartBusy(false);
    }
  };

  const ask = async (e: FormEvent) => {
    e.preventDefault();
    const question = message.trim();
    if (!question || busy) return;
    setBusy(true);
    setMessage("");
    const id = ++counter.current;
    try {
      const answers = await Promise.all(
        scope.map(async (m) => ({ member: m.member, answer: await m.client.chat(question) })),
      );
      setTurns((prev) => [...prev, { id, question, answers }]);
    } catch (err) {
      setTurns((prev) => [
        ...prev,
        { id, question, answers: [], error: err instanceof Error ? err.message : String(err) },
      ]);
    } finally {
      setBusy(false);
    }
  };

  const refresh = () => setTurns((prev) => [...prev]);

  return (
    <Stack>
      <Title order={2}>{t("nav.chat")}</Title>
      <Text c="dimmed" size="sm">{t("chat.intro")}</Text>
      {turns.length === 0 && (
        <Card withBorder padding="sm">
          <Text size="sm" fw={500}>{t("chat.examplesTitle")}</Text>
          <Group mt="xs" gap="xs">
            {[t("chat.example1"), t("chat.example2"), t("chat.example3")].map((example) => (
              <Button key={example} size="compact-xs" variant="light" onClick={() => setMessage(example)}>
                {example}
              </Button>
            ))}
          </Group>
        </Card>
      )}
      {turns.map((turn) => (
        <Stack key={turn.id} gap="xs">
          <Group justify="flex-end">
            <Card withBorder padding="xs" bg="var(--mantine-color-default-hover)">
              <Text size="sm">{turn.question}</Text>
            </Card>
          </Group>
          {turn.error && <Alert color="red">{t("app.error", { message: turn.error })}</Alert>}
          {visible(turn.answers).map(({ member, answer }) => (
            <Stack key={member.id} gap="xs">
              <Group gap="xs">
                {turn.answers.length > 1 && <Badge variant="outline">{member.title}</Badge>}
                <Text size="sm">{t(answer.answer, answer.params)}</Text>
              </Group>
              {answer.gates?.map((gate: Gate) => (
                <GateCard key={`${gate.case_id}:${gate.step_id}`} gate={{ ...gate, _member: member }} onDone={refresh} />
              ))}
              {answer.cases && answer.cases.length > 0 && (
                <Table striped withTableBorder>
                  <Table.Tbody>
                    {answer.cases.map((c: CaseRow) => (
                      <Table.Tr key={c.id}>
                        <Table.Td>
                          <Anchor component={Link} to={`/cases/${member.id}/${c.id}`}>
                            {pick(c.template_titles) || c.template_title}
                          </Anchor>
                        </Table.Td>
                        <Table.Td>{c.document?.original_name ?? c.document?.subject ?? "–"}</Table.Td>
                        <Table.Td>{t(`cases.status.${c.status}`)}</Table.Td>
                        <Table.Td>{formatDate(c.updated_at)}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              )}
              {answer.case && (
                <Card withBorder padding="sm">
                  <Group>
                    <Text fw={500}>{pick(answer.case.template_titles) || answer.case.template_title}</Text>
                    <Badge variant="light">{t(`cases.status.${answer.case.status}`)}</Badge>
                    <Anchor component={Link} to={`/cases/${member.id}/${answer.case.id}`} size="sm">
                      {t("chat.openCase")}
                    </Anchor>
                  </Group>
                  <Text size="sm" c="dimmed" mt={4}>
                    {answer.case.steps.length} {t("cases.steps")} · {answer.case.audit.length} {t("cases.audit")}
                  </Text>
                </Card>
              )}
              {(answer.start ? [answer.start] : answer.start_options ?? []).map((start) => (
                <Card key={start.process_id} withBorder padding="sm">
                  <Group justify="space-between">
                    <div>
                      <Text fw={500}>{pick(start.titles) || start.process_id}</Text>
                      <Text size="xs" c="dimmed">{Object.keys(start.contract).join(" · ") || t("solutions.start.noInputs")}</Text>
                    </div>
                    <Button size="xs" onClick={() => setStarting({ member, start })}>{t("chat.startForm")}</Button>
                  </Group>
                </Card>
              ))}
              {answer.intent === "help" && (
                <Card withBorder padding="sm">
                  <Text size="sm">{t("chat.helpText")}</Text>
                </Card>
              )}
            </Stack>
          ))}
        </Stack>
      ))}
      {showUpload && <UploadBox compact onDone={() => setShowUpload(false)} />}
      <StartDialog opened={starting !== null} processId={starting?.start.process_id ?? ""} contract={starting?.start.contract ?? {}} initial={starting?.start.prefill} busy={startBusy} onClose={() => setStarting(null)} onStart={startProcess} />
      <form onSubmit={(e) => void ask(e)}>
        <Group>
          <Button variant="default" onClick={() => setShowUpload((v) => !v)} aria-label={t("upload.title")}>
            {t("upload.attach")}
          </Button>
          <TextInput
            style={{ flex: 1 }}
            value={message}
            onChange={(e) => setMessage(e.currentTarget.value)}
            placeholder={t("chat.placeholder")}
            aria-label={t("nav.chat")}
          />
          <Button type="submit" loading={busy} disabled={!scope.length}>{t("chat.send")}</Button>
        </Group>
      </form>
    </Stack>
  );
}
