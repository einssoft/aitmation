// Alltag → Aufgaben (ADR-0049): the "tasks" collection of every member
// as a board - open, in progress, waiting, done. A task is a row like a
// contact is a row: versioned, searchable, written by people, the chat
// or a process (meeting minutes, mail intake). Nothing here is a second
// data model; the page reads and saves rows of the declared collection.
import { Alert, Badge, Button, Card, Group, Loader, Select, SimpleGrid, Stack, Text, TextInput, Textarea, Title } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";
import { type CollectionRow, type RecordDetail, type RecordField } from "../api/client";
import { useAcrossHousehold, useHousehold } from "../household";
import { useI18n } from "../i18n";

const COLLECTION = "tasks";
const COLUMNS = ["open", "in_progress", "waiting", "done"] as const;
const PRIORITY_COLOR: Record<string, string> = { low: "gray", normal: "blue", high: "orange", urgent: "red" };
const KIND_COLOR: Record<string, string> = { task: "teal", bug: "red", idea: "violet", question: "yellow" };

type TaskRow = CollectionRow & { _member: { id: string; title: string } };

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export function TasksPage() {
  const { t, pick, lang } = useI18n();
  const { members, selected } = useHousehold();
  const tasks = useAcrossHousehold<CollectionRow>((c) => c.rows(COLLECTION));
  const [schema, setSchema] = useState<RecordDetail | null>(null);
  const [kindFilter, setKindFilter] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Quick add
  const candidates = members.filter((m) => m.info && m.member.type !== "mailroom");
  const [target, setTarget] = useState<string | null>(null);
  const member = candidates.find((m) => m.member.id === (target ?? (selected !== "all" ? selected : candidates[0]?.member.id)));
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState("task");
  const [priority, setPriority] = useState("normal");
  const [due, setDue] = useState("");
  const [details, setDetails] = useState("");

  useEffect(() => {
    const client = candidates[0]?.client;
    if (!client || schema) return;
    client.record(COLLECTION).then(setSchema).catch(() => setSchema(null));
  }, [candidates, schema]);

  const field = (name: string): RecordField | undefined => schema?.fields.find((f) => f.name === name);
  const choiceLabel = (name: string, value: string): string => {
    const f = field(name);
    const labels = f?.choice_labels?.[value];
    return labels ? pick(labels) : value;
  };
  const options = (name: string) => (field(name)?.choices ?? []).map((v) => ({ value: v, label: choiceLabel(name, v) }));

  const rows = useMemo(() => {
    const list = tasks.rows as TaskRow[];
    return list.filter((r) => !kindFilter || r.values.kind === kindFilter);
  }, [tasks.rows, kindFilter]);

  const byStatus = (status: string) =>
    rows
      .filter((r) => (r.values.status ?? "open") === status || (status === "done" && r.values.status === "dropped"))
      .sort((a, b) => {
        const order = ["urgent", "high", "normal", "low"];
        const pa = order.indexOf(String(a.values.priority ?? "normal"));
        const pb = order.indexOf(String(b.values.priority ?? "normal"));
        if (pa !== pb) return pa - pb;
        return String(a.values.due ?? "9999").localeCompare(String(b.values.due ?? "9999"));
      });

  const save = async (row: TaskRow, values: Record<string, unknown>) => {
    const client = members.find((m) => m.member.id === row._member.id)?.client;
    if (!client) return;
    setBusy(row.row_id);
    setError(null);
    try {
      await client.saveRow(COLLECTION, row.row_id, { ...row.values, ...values });
      tasks.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const create = async () => {
    if (!member || !title.trim()) return;
    setBusy("new");
    setError(null);
    try {
      await member.client.createRow(COLLECTION, {
        title: title.trim(), kind, priority, status: "open", due, details: details.trim(), origin: "board",
      });
      setTitle("");
      setDue("");
      setDetails("");
      tasks.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const openCount = rows.filter((r) => !["done", "dropped"].includes(String(r.values.status ?? "open"))).length;
  const overdue = rows.filter((r) => r.values.due && String(r.values.due) < today() && !["done", "dropped"].includes(String(r.values.status ?? "open"))).length;

  return (
    <Stack gap="lg">
      <div>
        <Title order={2}>{t("board.title")}</Title>
        <Text c="dimmed" size="sm">{t("board.intro")}</Text>
      </div>

      <Card withBorder>
        <Stack gap="xs">
          <Text fw={600}>{t("board.new")}</Text>
          <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="sm">
            {candidates.length > 1 && (
              <Select label={t("household.members")} data={candidates.map((m) => ({ value: m.member.id, label: m.member.title }))} value={member?.member.id ?? null} onChange={setTarget} />
            )}
            <TextInput label={t("board.what")} placeholder={t("board.whatExample")} value={title} onChange={(e) => setTitle(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === "Enter") void create(); }} />
            <Select label={pick(field("kind")?.label ?? {}) || t("board.kind")} data={options("kind")} value={kind} onChange={(v) => setKind(v ?? "task")} />
            <Select label={pick(field("priority")?.label ?? {}) || t("board.priority")} data={options("priority")} value={priority} onChange={(v) => setPriority(v ?? "normal")} />
            <TextInput label={pick(field("due")?.label ?? {}) || t("board.due")} type="date" value={due} onChange={(e) => setDue(e.currentTarget.value)} />
          </SimpleGrid>
          <Textarea label={pick(field("details")?.label ?? {}) || t("board.details")} autosize minRows={1} value={details} onChange={(e) => setDetails(e.currentTarget.value)} />
          <Group>
            <Button loading={busy === "new"} disabled={!member || !title.trim()} onClick={() => void create()}>{t("board.create")}</Button>
            <Text size="xs" c="dimmed">{t("board.chatHint")}</Text>
          </Group>
        </Stack>
      </Card>

      {error && <Alert color="red">{error}</Alert>}
      {tasks.error && <Alert color="red">{tasks.error}</Alert>}

      <Group justify="space-between">
        <Group gap="xs">
          <Badge variant="light">{t("board.openCount", { count: openCount })}</Badge>
          {overdue > 0 && <Badge color="red" variant="light">{t("board.overdue", { count: overdue })}</Badge>}
        </Group>
        <Group gap="xs">
          <Select size="xs" clearable placeholder={t("board.allKinds")} data={options("kind")} value={kindFilter} onChange={setKindFilter} />
          <Button size="xs" variant={showDone ? "filled" : "default"} onClick={() => setShowDone((v) => !v)}>{t("board.showDone")}</Button>
        </Group>
      </Group>

      {tasks.loading && tasks.rows.length === 0 ? (
        <Loader size="sm" />
      ) : (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: showDone ? 4 : 3 }} spacing="md">
          {COLUMNS.filter((c) => showDone || c !== "done").map((status) => {
            const items = byStatus(status);
            return (
              <Stack key={status} gap="xs">
                <Group gap="xs">
                  <Text fw={600}>{choiceLabel("status", status)}</Text>
                  <Badge size="sm" variant="light" color="gray">{items.length}</Badge>
                </Group>
                {items.length === 0 && <Text size="sm" c="dimmed">{t("board.empty")}</Text>}
                {items.map((row) => {
                  const v = row.values as Record<string, string | undefined>;
                  const late = v.due && v.due < today() && status !== "done";
                  return (
                    <Card key={`${row._member.id}:${row.row_id}`} withBorder padding="sm" style={late ? { borderColor: "var(--mantine-color-red-6)" } : undefined}>
                      <Stack gap={6}>
                        <Group gap={6} wrap="wrap">
                          <Badge size="xs" color={KIND_COLOR[v.kind ?? "task"] ?? "gray"} variant="light">{choiceLabel("kind", v.kind ?? "task")}</Badge>
                          <Badge size="xs" color={PRIORITY_COLOR[v.priority ?? "normal"] ?? "gray"} variant="light">{choiceLabel("priority", v.priority ?? "normal")}</Badge>
                          {v.due && <Badge size="xs" color={late ? "red" : "gray"} variant="outline">{new Intl.DateTimeFormat(lang, { dateStyle: "medium" }).format(new Date(v.due))}</Badge>}
                          {members.length > 1 && <Badge size="xs" variant="outline" color="gray">{row._member.title}</Badge>}
                        </Group>
                        <Text fw={500} size="sm">{v.title}</Text>
                        {v.details && <Text size="xs" c="dimmed" lineClamp={3}>{v.details}</Text>}
                        <Group gap={6} justify="space-between">
                          <Text size="xs" c="dimmed">{[v.assignee, v.area, v.origin && v.origin !== "board" ? `${t("board.from")} ${v.origin}` : ""].filter(Boolean).join(" · ")}</Text>
                          <Select size="xs" w={140} data={options("status")} value={v.status ?? "open"} disabled={busy === row.row_id} onChange={(next) => { if (next && next !== v.status) void save(row, { status: next }); }} />
                        </Group>
                      </Stack>
                    </Card>
                  );
                })}
              </Stack>
            );
          })}
        </SimpleGrid>
      )}
    </Stack>
  );
}
