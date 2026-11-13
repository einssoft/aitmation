// Einstellungen → Dokumenttypen: the instance's own declarations. Types
// from an app bundle are shown read-only until the household overlays
// them; the overlay and its own types live in the local bundle and can be
// removed again (falling back to the bundle's version). The form is the
// declaration - one field per TOML key, four languages (ADR-0022).
import { Alert, Badge, Button, Card, Group, Select, Stack, Table, Text, Textarea, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { type DoctypeRow, type TemplateRow } from "../api/client";
import { useHousehold } from "../household";
import { LANGUAGES, LANGUAGE_NAMES, useI18n } from "../i18n";

type Draft = {
  id: string;
  title: Record<string, string>;
  description: Record<string, string>;
  features: string;
  chain: string;
  fields: string;
};

const empty = (): Draft => ({ id: "", title: {}, description: {}, features: "", chain: "", fields: "" });

function toDraft(row: DoctypeRow): Draft {
  return {
    id: row.id,
    title: { ...row.title },
    description: { ...row.description },
    features: row.features.join("\n"),
    chain: row.chain,
    fields: Object.entries(row.fields)
      .map(([k, v]) => `${k} = ${v}`)
      .join("\n"),
  };
}

function parseFields(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const idx = line.indexOf("=");
    if (idx > 0) out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return out;
}

export function DoctypesPage() {
  const { t, pick } = useI18n();
  const { members, selected } = useHousehold();
  const candidates = members.filter((m) => m.info && m.member.type !== "mailroom");
  const state = candidates.find((m) => m.member.id === (selected !== "all" ? selected : candidates[0]?.member.id));
  const [rows, setRows] = useState<DoctypeRow[]>([]);
  const [templates, setTemplates] = useState<TemplateRow[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!state) return;
    Promise.all([state.client.doctypes(), state.client.templates()])
      .then(([d, tpl]) => {
        setRows(d);
        setTemplates(tpl);
        setError(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [state]);
  useEffect(load, [load]);

  const save = async () => {
    if (!state || !draft) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await state.client.saveDoctype(draft.id.trim(), {
        title: draft.title,
        description: draft.description,
        features: draft.features.split("\n").map((f) => f.trim()).filter(Boolean),
        chain: draft.chain,
        fields: parseFields(draft.fields),
      });
      setMessage(t("doctypes.saved"));
      setDraft(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: DoctypeRow) => {
    if (!state) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await state.client.deleteDoctype(row.id);
      setMessage(result.fallback ? t("doctypes.reset") : t("doctypes.removed"));
      setDraft(null);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const chains = templates.filter((tpl) => !tpl.intake);
  const chainOptions = chains.map((tpl) => ({ value: tpl.id, label: `${pick(tpl.title) || tpl.id}${tpl.app ? ` · ${tpl.app}` : ""}` }));

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("doctypes.title")}</Title>
        <Button size="xs" onClick={() => { setDraft({ ...empty(), chain: chains[0]?.id ?? "" }); setIsNew(true); }}>
          {t("doctypes.add")}
        </Button>
      </Group>
      <Text c="dimmed" size="sm">{t("doctypes.intro")}</Text>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {message && <Alert color="green">{message}</Alert>}

      <Table striped highlightOnHover>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{t("doctypes.id")}</Table.Th>
            <Table.Th>{t("doctypes.name")}</Table.Th>
            <Table.Th>{t("doctypes.chain")}</Table.Th>
            <Table.Th>{t("doctypes.source")}</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => (
            <Table.Tr key={row.id}>
              <Table.Td><code>{row.id}</code></Table.Td>
              <Table.Td>{pick(row.title)}</Table.Td>
              <Table.Td><code>{row.chain}</code></Table.Td>
              <Table.Td>
                {row.source === "local" ? (
                  <Badge variant="light" color="teal">{row.overrides ? t("doctypes.overlays", { app: row.overrides }) : t("doctypes.own")}</Badge>
                ) : (
                  <Badge variant="outline" color="gray">{row.source}</Badge>
                )}
              </Table.Td>
              <Table.Td>
                <Group gap="xs" justify="flex-end">
                  <Button size="compact-xs" variant="subtle" onClick={() => { setDraft(toDraft(row)); setIsNew(false); }}>
                    {row.source === "local" ? t("doctypes.edit") : t("doctypes.adapt")}
                  </Button>
                  {row.source === "local" && (
                    <Button size="compact-xs" variant="subtle" color="red" loading={busy} onClick={() => void remove(row)}>
                      {row.overrides ? t("doctypes.resetAction") : t("doctypes.remove")}
                    </Button>
                  )}
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>

      {draft && (
        <Card withBorder>
          <Stack gap="sm">
            <Title order={4}>{isNew ? t("doctypes.add") : t("doctypes.editing", { id: draft.id })}</Title>
            {isNew && (
              <TextInput
                label={t("doctypes.id")}
                description={t("doctypes.idHelp")}
                value={draft.id}
                onChange={(e) => setDraft({ ...draft, id: e.currentTarget.value.toLowerCase() })}
              />
            )}
            <Group grow>
              {LANGUAGES.map((l) => (
                <TextInput
                  key={l}
                  label={`${t("doctypes.name")} · ${LANGUAGE_NAMES[l]}`}
                  required={l === "de"}
                  value={draft.title[l] ?? ""}
                  onChange={(e) => setDraft({ ...draft, title: { ...draft.title, [l]: e.currentTarget.value } })}
                />
              ))}
            </Group>
            <Group grow>
              {LANGUAGES.map((l) => (
                <TextInput
                  key={l}
                  label={`${t("doctypes.description")} · ${LANGUAGE_NAMES[l]}`}
                  value={draft.description[l] ?? ""}
                  onChange={(e) => setDraft({ ...draft, description: { ...draft.description, [l]: e.currentTarget.value } })}
                />
              ))}
            </Group>
            <Textarea
              label={t("doctypes.features")}
              description={t("doctypes.featuresHelp")}
              autosize
              minRows={3}
              value={draft.features}
              onChange={(e) => setDraft({ ...draft, features: e.currentTarget.value })}
            />
            <Select
              label={t("doctypes.chain")}
              description={t("doctypes.chainHelp")}
              data={chainOptions}
              value={draft.chain}
              onChange={(v) => setDraft({ ...draft, chain: v ?? "" })}
              searchable
            />
            <Textarea
              label={t("doctypes.fields")}
              description={t("doctypes.fieldsHelp")}
              autosize
              minRows={2}
              value={draft.fields}
              onChange={(e) => setDraft({ ...draft, fields: e.currentTarget.value })}
            />
            <Group>
              <Button loading={busy} disabled={!draft.id.trim() || !draft.title.de?.trim() || !draft.chain} onClick={() => void save()}>
                {t("app.save")}
              </Button>
              <Button variant="default" onClick={() => setDraft(null)}>{t("app.cancel")}</Button>
            </Group>
          </Stack>
        </Card>
      )}
    </Stack>
  );
}
