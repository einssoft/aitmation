// Arbeiten → Fristen: what is coming (waiting timers across the household),
// a hand-set follow-up ("look at this again on the 30th"), and the
// templates that run on the calendar (Stufe 1.3). Time is a trigger like
// a folder is - the gate at the end stays the gate.
import { Alert, Badge, Button, Card, Code, Group, Select, SimpleGrid, Stack, Table, Text, TextInput, Textarea, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Anchor } from "@mantine/core";
import { type DeadlineRow, type ScheduleRow } from "../api/client";
import { useAcrossHousehold, useHousehold } from "../household";
import { useI18n } from "../i18n";

export function FollowUpForm({ memberId, aboutCase, compact, onDone }: { memberId?: string; aboutCase?: string; compact?: boolean; onDone?: () => void }) {
  const { t } = useI18n();
  const { members, selected } = useHousehold();
  const candidates = members.filter((m) => m.info && m.member.type !== "mailroom");
  const [chosen, setTarget] = useState<string | null>(null);
  // Members arrive asynchronously; resolve the target at render time.
  const target = memberId ?? chosen ?? (selected !== "all" ? selected : candidates[0]?.member.id ?? null);
  const [title, setTitle] = useState("");
  const [date, setDate] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const state = candidates.find((m) => m.member.id === target);

  const save = async () => {
    if (!state) return;
    setBusy(true);
    setMessage(null);
    try {
      await state.client.createFollowUp({ title: title.trim(), due_at: date, note: note.trim(), about_case: aboutCase ?? null });
      setMessage({ kind: "ok", text: t("deadlines.created") });
      setTitle("");
      setDate("");
      setNote("");
      onDone?.();
    } catch (err) {
      setMessage({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack gap="xs">
      <SimpleGrid cols={{ base: 1, sm: compact ? 2 : 3 }} spacing="sm">
        {!memberId && candidates.length > 1 && (
          <Select label={t("household.members")} data={candidates.map((m) => ({ value: m.member.id, label: m.member.title }))} value={target} onChange={setTarget} />
        )}
        <TextInput label={t("deadlines.what")} placeholder={t("deadlines.whatExample")} value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
        <TextInput label={t("deadlines.when")} type="date" value={date} onChange={(e) => setDate(e.currentTarget.value)} />
      </SimpleGrid>
      <Textarea label={t("deadlines.note")} autosize minRows={1} value={note} onChange={(e) => setNote(e.currentTarget.value)} />
      {message && <Alert color={message.kind === "ok" ? "green" : "red"}>{message.text}</Alert>}
      <Group>
        <Button loading={busy} disabled={!state || !title.trim() || !date} onClick={() => void save()}>{t("deadlines.create")}</Button>
      </Group>
    </Stack>
  );
}

export function DeadlinesPage() {
  const { t, pick, formatDate } = useI18n();
  const { members } = useHousehold();
  const deadlines = useAcrossHousehold<DeadlineRow>((c) => c.deadlines());
  const schedules = useAcrossHousehold<ScheduleRow>((c) => c.schedules());
  const [running, setRunning] = useState<string | null>(null);
  const sorted = [...deadlines.rows].sort((a, b) => (a.deadline_at < b.deadline_at ? -1 : 1));
  const now = new Date().toISOString();

  const runNow = async (row: ScheduleRow & { _member: { id: string } }) => {
    const client = members.find((m) => m.member.id === row._member.id)?.client;
    if (!client) return;
    setRunning(`${row._member.id}:${row.template_id}`);
    try {
      await client.runSchedule(row.template_id);
      schedules.refresh();
    } finally {
      setRunning(null);
    }
  };

  return (
    <Stack>
      <Title order={2}>{t("deadlines.title")}</Title>
      <Text c="dimmed" size="sm">{t("deadlines.intro")}</Text>
      {deadlines.error && <Alert color="red">{t("app.error", { message: deadlines.error })}</Alert>}

      <Card withBorder>
        <Title order={5}>{t("deadlines.new")}</Title>
        <FollowUpForm onDone={deadlines.refresh} />
      </Card>

      <Card withBorder>
        <Title order={5}>{t("deadlines.upcoming")}</Title>
        {!sorted.length && <Text c="dimmed" size="sm" mt="xs">{t("deadlines.empty")}</Text>}
        {sorted.length > 0 && (
          <Table striped mt="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("deadlines.when")}</Table.Th>
                <Table.Th>{t("deadlines.what")}</Table.Th>
                <Table.Th>{t("household.members")}</Table.Th>
                <Table.Th>{t("cases.template")}</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sorted.map((d) => (
                <Table.Tr key={`${d._member.id}:${d.case_id}`}>
                  <Table.Td>
                    {formatDate(d.deadline_at)}
                    {d.deadline_at <= now && <Badge ml="xs" size="xs" color="red" variant="light">{t("deadlines.due")}</Badge>}
                  </Table.Td>
                  <Table.Td>
                    {d.title ?? "–"}
                    {d.note && <Text size="xs" c="dimmed">{d.note}</Text>}
                  </Table.Td>
                  <Table.Td>{d._member.title}</Table.Td>
                  <Table.Td>{pick(d.template_title)}</Table.Td>
                  <Table.Td>
                    <Group gap="xs" justify="flex-end">
                      <Anchor component={Link} to={`/cases/${d._member.id}/${d.case_id}`} size="sm">{t("chat.openCase")}</Anchor>
                      {d.about_case && <Anchor component={Link} to={`/cases/${d._member.id}/${d.about_case}`} size="sm">{t("deadlines.aboutCase")}</Anchor>}
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>

      <Card withBorder>
        <Title order={5}>{t("deadlines.schedules")}</Title>
        <Text c="dimmed" size="sm" mt="xs">{t("deadlines.schedulesHelp")}</Text>
        {!schedules.rows.length && <Text c="dimmed" size="sm" mt="xs">{t("deadlines.noSchedules")}</Text>}
        {schedules.rows.length > 0 && (
          <Table striped mt="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("cases.template")}</Table.Th>
                <Table.Th>{t("household.members")}</Table.Th>
                <Table.Th>{t("deadlines.rhythm")}</Table.Th>
                <Table.Th>{t("deadlines.nextRun")}</Table.Th>
                <Table.Th>{t("deadlines.lastRun")}</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {schedules.rows.map((s) => (
                <Table.Tr key={`${s._member.id}:${s.template_id}`}>
                  <Table.Td>{s.title ? pick(s.title) : <Code>{s.template_id}</Code>}</Table.Td>
                  <Table.Td>{s._member.title}</Table.Td>
                  <Table.Td>
                    {s.error ? <Text c="red" size="sm">{s.error}</Text> : t(`deadlines.every_${s.every}`, { at: s.at ?? "", weekday: s.weekday ? t(`deadlines.weekday_${s.weekday}`) : "", day: s.day ?? "" })}
                  </Table.Td>
                  <Table.Td>{s.next_at ? formatDate(s.next_at) : "–"}</Table.Td>
                  <Table.Td>{s.last_fired_at ? formatDate(s.last_fired_at) : "–"}</Table.Td>
                  <Table.Td>
                    <Button size="compact-xs" variant="subtle" loading={running === `${s._member.id}:${s.template_id}`} onClick={() => void runNow(s)}>{t("deadlines.runNow")}</Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Stack>
  );
}
