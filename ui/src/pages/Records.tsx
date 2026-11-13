// Einstellungen → Stammdaten: the instance's declared records (ADR-0026).
// The form is generated from the schema - fields, groups, the country
// variant - and never knows what a UID is. A single record (the company
// profile) is one form; a collection (contacts, accounts) is a searchable
// table with the same generated form per row. Saving appends a version;
// who changed what when stays visible for a revisor.
import { Alert, Badge, Button, Card, Group, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useMemo, useState } from "react";
import { type CollectionRow, type RecordDetail, type RecordField, type RecordRow } from "../api/client";
import { useHousehold, type MemberState } from "../household";
import { useI18n } from "../i18n";

const GROUP_ORDER = ["identity", "address", "contact", "tax", "bank", ""];

/** Resolve the effective field list for the values as they are being
 * edited: common fields, then the chosen variant's additions/overrides. */
export function resolveFields(detail: RecordDetail, values: Record<string, unknown>): RecordField[] {
  const out = new Map<string, RecordField>();
  for (const f of detail.common_fields) out.set(f.name, f);
  if (detail.variant_field) {
    const variant = String(values[detail.variant_field] ?? "");
    for (const f of detail.variant_fields[variant] ?? []) out.set(f.name, f);
  }
  return Array.from(out.values());
}

export function useRecordMember(): MemberState | undefined {
  const { members, selected } = useHousehold();
  const candidates = members.filter((m) => m.info && m.member.type !== "mailroom");
  return candidates.find((m) => m.member.id === (selected !== "all" ? selected : candidates[0]?.member.id));
}

function withDefaults(detail: RecordDetail, values: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...values };
  for (const f of detail.common_fields) if (out[f.name] === undefined && f.default != null) out[f.name] = f.default;
  return out;
}

/** The generated fields of a schema, grouped, bound to a values object. */
export function SchemaFields({ detail, values, onChange }: { detail: RecordDetail; values: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void }) {
  const { t, pick } = useI18n();
  const fields = useMemo(() => resolveFields(detail, values), [detail, values]);
  const groups = useMemo(() => {
    const byGroup = new Map<string, RecordField[]>();
    for (const f of fields) {
      const list = byGroup.get(f.group) ?? [];
      list.push(f);
      byGroup.set(f.group, list);
    }
    return Array.from(byGroup.entries()).sort(([a], [b]) => GROUP_ORDER.indexOf(a) - GROUP_ORDER.indexOf(b));
  }, [fields]);
  const set = (name: string, value: unknown) => onChange({ ...values, [name]: value });
  const field = (f: RecordField) => {
    const label = pick(f.label) + (f.required ? " *" : "");
    const help = f.help && Object.keys(f.help).length ? pick(f.help) : f.example ? t("records.example", { example: f.example }) : undefined;
    const value = values[f.name];
    switch (f.type) {
      case "bool":
        return <Switch key={f.name} label={label} description={help} checked={Boolean(value)} onChange={(e) => set(f.name, e.currentTarget.checked)} />;
      case "choice":
        return (
          <Select
            key={f.name}
            label={label}
            description={help}
            data={f.choices.map((c) => ({ value: c, label: f.choice_labels[c] ? pick(f.choice_labels[c]) : c }))}
            value={typeof value === "string" ? value : null}
            onChange={(v) => set(f.name, v)}
            searchable={f.choices.length > 8}
            clearable={!f.required}
          />
        );
      default:
        return (
          <TextInput
            key={f.name}
            label={label}
            description={help}
            type={f.type === "email" ? "email" : "text"}
            placeholder={f.example || undefined}
            value={value == null ? "" : String(value)}
            onChange={(e) => set(f.name, e.currentTarget.value)}
          />
        );
    }
  };
  return (
    <>
      {groups.map(([group, list]) => (
        <div key={group}>
          {group && <Text size="xs" fw={700} c="dimmed" tt="uppercase" mb={4}>{t(`records.group.${group}`)}</Text>}
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">{list.map(field)}</SimpleGrid>
        </div>
      ))}
    </>
  );
}

export function RecordForm({ member, recordId, onSaved, compact }: { member: MemberState; recordId: string; onSaved?: (d: RecordDetail) => void; compact?: boolean }) {
  const { t, pick, formatDate } = useI18n();
  const [detail, setDetail] = useState<RecordDetail | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(() => {
    member.client
      .record(recordId)
      .then((d) => {
        setDetail(d);
        setValues(withDefaults(d, d.values));
        setError(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [member, recordId]);
  useEffect(load, [load]);

  const save = async () => {
    if (!detail) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await member.client.saveRecord(recordId, values);
      setDetail(result);
      setValues({ ...result.values });
      setSaved(true);
      onSaved?.(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!detail) return error ? <Alert color="red">{t("app.error", { message: error })}</Alert> : <Text c="dimmed">{t("app.loading")}</Text>;

  return (
    <Stack gap="sm">
      {!compact && detail.description && <Text size="sm" c="dimmed">{pick(detail.description)}</Text>}
      <SchemaFields detail={detail} values={values} onChange={setValues} />
      {error && <Alert color="red">{error}</Alert>}
      {saved && <Alert color="green">{t("records.saved")}</Alert>}
      <Group justify="space-between">
        <Text size="xs" c="dimmed">
          {detail.meta ? t("records.lastSaved", { when: formatDate(detail.meta.saved_at), who: detail.meta.saved_by, version: detail.meta.version }) : t("records.never")}
        </Text>
        <Button loading={busy} onClick={() => void save()}>{t("app.save")}</Button>
      </Group>
    </Stack>
  );
}

/** A collection: searchable table plus the generated form per row. */
export function CollectionView({ member, recordId, onChanged }: { member: MemberState; recordId: string; onChanged?: () => void }) {
  const { t, pick, formatDate } = useI18n();
  const [detail, setDetail] = useState<RecordDetail | null>(null);
  const [rows, setRows] = useState<CollectionRow[]>([]);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<{ rowId: string | null; values: Record<string, unknown> } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    member.client.record(recordId).then(setDetail).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [member, recordId]);
  const load = useCallback(() => {
    member.client.rows(recordId, query).then(setRows).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [member, recordId, query]);
  useEffect(load, [load]);

  const save = async () => {
    if (!editing || !detail) return;
    setBusy(true);
    setError(null);
    try {
      if (editing.rowId) await member.client.saveRow(recordId, editing.rowId, editing.values);
      else await member.client.createRow(recordId, editing.values);
      setEditing(null);
      load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const remove = async (rowId: string) => {
    setBusy(true);
    try {
      await member.client.deleteRow(recordId, rowId);
      load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!detail) return error ? <Alert color="red">{t("app.error", { message: error })}</Alert> : <Text c="dimmed">{t("app.loading")}</Text>;
  // The table shows the first few fields; the form shows all.
  const columns = detail.common_fields.filter((f) => f.type !== "bool").slice(0, 5);
  const show = (f: RecordField, v: unknown) => (f.type === "choice" && typeof v === "string" && f.choice_labels[v] ? pick(f.choice_labels[v]) : v == null ? "" : String(v));

  return (
    <Stack gap="sm">
      {detail.description && <Text size="sm" c="dimmed">{pick(detail.description)}</Text>}
      <Group>
        <TextInput style={{ flex: 1 }} placeholder={t("records.search")} value={query} onChange={(e) => setQuery(e.currentTarget.value)} />
        <Button onClick={() => setEditing({ rowId: null, values: withDefaults(detail, {}) })}>{t("records.addRow")}</Button>
      </Group>
      {error && <Alert color="red">{error}</Alert>}
      {rows.length === 0 && <Text size="sm" c="dimmed">{t("records.noRows")}</Text>}
      {rows.length > 0 && (
        <Table striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              {columns.map((f) => <Table.Th key={f.name}>{pick(f.label)}</Table.Th>)}
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.map((r) => (
              <Table.Tr key={r.row_id}>
                {columns.map((f) => <Table.Td key={f.name}>{show(f, r.values[f.name])}</Table.Td>)}
                <Table.Td>
                  <Group gap={4} justify="flex-end" wrap="nowrap">
                    <Button size="compact-xs" variant="subtle" onClick={() => setEditing({ rowId: r.row_id, values: withDefaults(detail, r.values) })}>{t("doctypes.edit")}</Button>
                    <Button size="compact-xs" variant="subtle" color="red" loading={busy} onClick={() => void remove(r.row_id)}>{t("doctypes.remove")}</Button>
                  </Group>
                  <Text size="xs" c="dimmed" ta="right">{t("records.rowMeta", { version: r.version, when: formatDate(r.saved_at), who: r.saved_by })}</Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      {editing && (
        <Card withBorder>
          <Stack gap="sm">
            <Title order={5}>{editing.rowId ? t("records.editRow") : t("records.addRow")}</Title>
            <SchemaFields detail={detail} values={editing.values} onChange={(v) => setEditing({ ...editing, values: v })} />
            <Group>
              <Button loading={busy} onClick={() => void save()}>{t("app.save")}</Button>
              <Button variant="default" onClick={() => setEditing(null)}>{t("app.cancel")}</Button>
            </Group>
          </Stack>
        </Card>
      )}
    </Stack>
  );
}

export function RecordsPage() {
  const { t, pick } = useI18n();
  const member = useRecordMember();
  const [rows, setRows] = useState<RecordRow[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!member) return;
    member.client
      .records()
      .then((r) => {
        setRows(r);
        setCurrent((c) => c && r.some((x) => x.id === c) ? c : r[0]?.id ?? null);
        setError(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [member]);
  useEffect(load, [load]);

  return (
    <Stack>
      <Title order={2}>{t("records.title")}</Title>
      <Text c="dimmed" size="sm">{t("records.intro")}</Text>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {!member && <Text c="dimmed">{t("records.noMember")}</Text>}
      {member && rows.length === 0 && !error && <Text c="dimmed">{t("records.empty")}</Text>}
      {rows.length > 1 && (
        <Group gap="xs">
          {rows.map((r) => (
            <Button key={r.id} size="xs" variant={r.id === current ? "filled" : "light"} onClick={() => setCurrent(r.id)}>
              {pick(r.title)}{r.kind === "collection" && typeof r.rows === "number" ? ` (${r.rows})` : ""}
            </Button>
          ))}
        </Group>
      )}
      {member && rows.filter((r) => r.id === current).map((r) => (
        <Card key={`${member.member.id}:${r.id}`} withBorder>
          <Group justify="space-between" mb="sm">
            <Group gap="xs">
              <Title order={4}>{pick(r.title)}</Title>
              <Badge variant="outline" color="gray">{r.source}</Badge>
            </Group>
            {r.completeness && (
              <Badge color={r.completeness.complete ? "green" : "yellow"} variant="light">
                {r.completeness.complete ? t("records.complete") : t("records.incomplete", { filled: r.completeness.filled, required: r.completeness.required })}
              </Badge>
            )}
          </Group>
          {r.kind === "collection" ? <CollectionView member={member} recordId={r.id} onChanged={load} /> : <RecordForm member={member} recordId={r.id} onSaved={load} />}
        </Card>
      ))}
    </Stack>
  );
}
