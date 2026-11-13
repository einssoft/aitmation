// Einstellungen → Modelle: the registry as the owner's release record
// (ADR-0017), now editable, with provider, region and data processing
// agreement per deployment and the legal space derived from the profile's
// country (ADR-0027). Keys are set here too - and never shown again.
import { Alert, Anchor, Badge, Button, Card, Code, Group, NumberInput, PasswordInput, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput, Textarea, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { type ModelBody, type ModelRow, type ModelsContext, type ModelTestResult } from "../api/client";
import { useI18n } from "../i18n";
import { useRecordMember } from "./Records";

const CLASSES = ["public", "internal", "confidential", "sensitive"];
const CLASS_COLOR: Record<string, string> = { public: "gray", internal: "blue", confidential: "orange", sensitive: "red" };
const SECRET_NAMES = ["anthropic_api_key", "openai_api_key"] as const;
// Table cells squeeze badges into ellipses; a badge that names a data class
// must stay readable whatever the column width.
const NO_TRUNCATE = { root: { overflow: "visible", maxWidth: "none", minWidth: "max-content" }, label: { overflow: "visible" } } as const;

const emptyBody = (): ModelBody => ({
  model: "", provider: "anthropic", base_url: "", clearance: "internal", role: "cheap", status: "experimental",
  cost_in_chf: 0, cost_out_chf: 0, local: false, region: "US", dpa: false, vision: false, note: "",
});

function toBody(row: ModelRow): ModelBody {
  const { id: _id, permitted_up_to: _p, missing_secret: _m, ...body } = row;
  return body;
}

export function ModelsPage() {
  const { t, formatNumber } = useI18n();
  const member = useRecordMember();
  const [rows, setRows] = useState<ModelRow[]>([]);
  const [context, setContext] = useState<ModelsContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [draftId, setDraftId] = useState("");
  const [draft, setDraft] = useState<ModelBody | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, ModelTestResult>>({});
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [newSecret, setNewSecret] = useState<{ name: string; value: string }>({ name: "", value: "" });

  const load = useCallback(() => {
    if (!member) return;
    Promise.all([member.client.models(), member.client.modelsContext()])
      .then(([m, c]) => {
        setRows(m);
        setContext(c);
        setError(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [member]);
  useEffect(load, [load]);

  const run = async (key: string, fn: () => Promise<void>, done?: string) => {
    setBusy(key);
    setError(null);
    setMessage(null);
    try {
      await fn();
      if (done) setMessage(done);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  // The provider's key lives once per instance (secrets), not per entry -
  // but the person sets it where the entry is edited, so a key typed into
  // the form is saved first, then the entry.
  const SECRET_FOR_PROVIDER: Record<string, string> = { anthropic: "anthropic_api_key", openai: "openai_api_key" };
  const [draftKey, setDraftKey] = useState("");
  const draftSecret = draft ? SECRET_FOR_PROVIDER[draft.provider] : undefined;
  const draftSecretStatus = draftSecret ? (context?.secrets[draftSecret] ?? "none") : "none";

  const save = () => {
    if (!member || !draft) return;
    const body = { ...draft };
    if (body.provider === "ollama") { body.local = true; body.region = "local"; }
    else if (body.region === "local") { body.local = false; body.region = "US"; }
    const key = draftKey.trim();
    void run("save", async () => {
      if (draftSecret && key) await member.client.setSecret(draftSecret, key);
      await member.client.saveModel(draftId.trim(), body);
      setDraft(null);
      setDraftKey("");
    }, t("settings.models.saved"));
  };

  const test = (id: string) => {
    if (!member) return;
    void run(`test:${id}`, async () => {
      const result = await member.client.testModel(id);
      setTests((prev) => ({ ...prev, [id]: result }));
    });
  };

  const setKey = (name: string, value: string) => {
    if (!member) return;
    void run(`key:${name}`, async () => { await member.client.setSecret(name, value); setKeys((k) => ({ ...k, [name]: "" })); }, value ? t("settings.models.keySaved") : t("settings.models.keyCleared"));
  };

  const testText = (r: ModelTestResult) => {
    if (r.ok) return t("settings.models.testOk", { model: r.model_ref ?? "" });
    if (r.reason === "missing_secret") return t("settings.models.testMissingSecret", { secret: r.secret ?? "" });
    if (r.reason === "model_not_pulled") return t("settings.models.testNotPulled", { available: (r.available ?? []).join(", ") || "–" });
    return t("settings.models.testFailed", { detail: r.detail ?? "" });
  };

  if (!member) return <Text c="dimmed">{t("records.noMember")}</Text>;
  const jurisdiction = context?.jurisdiction ?? null;

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("settings.models.title")}</Title>
        <Button size="xs" onClick={() => { setDraft(emptyBody()); setDraftId(""); setIsNew(true); }}>{t("settings.models.add")}</Button>
      </Group>
      <Text c="dimmed" size="sm">{t("settings.models.intro")}</Text>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {message && <Alert color="green">{message}</Alert>}

      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <Card withBorder>
          <Title order={5}>{t("settings.models.legalTitle")}</Title>
          {!context?.country && (
            <Text size="sm" mt="xs">
              {t("settings.models.legalNoCountry")}{" "}
              <Anchor component={Link} to="/knowledge/records" size="sm">{t("nav.records")}</Anchor>
            </Text>
          )}
          {context?.country && !jurisdiction && <Text size="sm" mt="xs">{t("settings.models.legalUnknown", { country: context.country })}</Text>}
          {jurisdiction && (
            <Stack gap={4} mt="xs">
              <Text size="sm"><b>{t("settings.models.country")}:</b> {jurisdiction.country} · {jurisdiction.law}</Text>
              <Text size="sm">{t("settings.models.legalRule", { from: t(`settings.models.class_${jurisdiction.dpa_required_from}`), regions: jurisdiction.allowed_regions.join(", ") })}</Text>
              <Text size="xs" c="dimmed">{t("settings.models.legalNote")}</Text>
            </Stack>
          )}
        </Card>
        <Card withBorder>
          <Title order={5}>{t("settings.models.keysTitle")}</Title>
          <Text size="xs" c="dimmed" mb="xs">{t("settings.models.keysIntro")}</Text>
          <Stack gap="xs">
            {[...SECRET_NAMES, ...Object.keys(context?.secrets ?? {}).filter((n) => !(SECRET_NAMES as readonly string[]).includes(n))].map((name) => {
              const status = context?.secrets[name] ?? "none";
              const known = (SECRET_NAMES as readonly string[]).includes(name);
              return (
                <Group key={name} align="flex-end" gap="xs" wrap="nowrap">
                  <PasswordInput
                    style={{ flex: 1 }}
                    size="xs"
                    label={known ? t(`settings.models.key_${name}`) : name}
                    placeholder={status === "none" ? t("settings.models.keyUnset") : t(`settings.models.keyFrom_${status}`)}
                    value={keys[name] ?? ""}
                    onChange={(e) => setKeys((k) => ({ ...k, [name]: e.currentTarget.value }))}
                    disabled={status === "env"}
                    autoComplete="off"
                  />
                  <Badge variant="light" color={status === "none" ? "gray" : "green"}>{t(`settings.models.keyFrom_${status}`)}</Badge>
                  <Button size="xs" disabled={!keys[name] || status === "env"} loading={busy === `key:${name}`} onClick={() => setKey(name, keys[name] ?? "")}>{t("app.save")}</Button>
                  {status === "file" && <Button size="xs" variant="subtle" color="red" onClick={() => setKey(name, "")}>{t("settings.models.keyRemove")}</Button>}
                </Group>
              );
            })}
            <Group align="flex-end" gap="xs" wrap="nowrap">
              <TextInput size="xs" label={t("settings.models.newSecretName")} description={t("settings.models.newSecretHelp")} placeholder="scheduler_token" value={newSecret.name} onChange={(e) => setNewSecret((v) => ({ ...v, name: e.currentTarget.value.toLowerCase() }))} />
              <PasswordInput size="xs" style={{ flex: 1 }} label={t("settings.models.newSecretValue")} value={newSecret.value} onChange={(e) => setNewSecret((v) => ({ ...v, value: e.currentTarget.value }))} autoComplete="off" />
              <Button size="xs" disabled={!/^[a-z][a-z0-9_]{1,60}$/.test(newSecret.name) || !newSecret.value} loading={busy === `key:${newSecret.name}`} onClick={() => { setKey(newSecret.name, newSecret.value); setNewSecret({ name: "", value: "" }); }}>{t("app.save")}</Button>
            </Group>
          </Stack>
        </Card>
      </SimpleGrid>

      <Table striped highlightOnHover>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>ID</Table.Th>
            <Table.Th>{t("settings.models.model")}</Table.Th>
            <Table.Th>{t("settings.models.provider")}</Table.Th>
            <Table.Th>{t("settings.models.region")}</Table.Th>
            <Table.Th>{t("settings.models.clearance")}</Table.Th>
            <Table.Th>{t("settings.models.permitted")}</Table.Th>
            <Table.Th>{t("settings.models.role")}</Table.Th>
            <Table.Th>{t("settings.models.status")}</Table.Th>
            <Table.Th>{t("settings.models.cost")}</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((m) => (
            <Table.Tr key={m.id}>
              <Table.Td><Code>{m.id}</Code></Table.Td>
              <Table.Td>
                <Code>{m.model}</Code>
                {m.vision && <Badge ml={4} size="xs" variant="outline">{t("settings.models.vision")}</Badge>}
                {m.missing_secret && <Badge ml={4} size="xs" color="red" variant="light">{t("settings.models.keyMissing")}</Badge>}
              </Table.Td>
              <Table.Td>{t(`settings.models.provider_${m.provider}`)}</Table.Td>
              <Table.Td>
                {m.local ? t("settings.models.local") : m.region}
                {!m.local && <Badge ml={4} size="xs" variant="light" color={m.dpa ? "green" : "gray"}>{m.dpa ? t("settings.models.dpaYes") : t("settings.models.dpaNo")}</Badge>}
              </Table.Td>
              <Table.Td><Badge variant="light" color={CLASS_COLOR[m.clearance]} styles={NO_TRUNCATE}>{t(`settings.models.class_${m.clearance}`)}</Badge></Table.Td>
              <Table.Td>
                <Badge variant={m.permitted_up_to === m.clearance ? "light" : "filled"} color={CLASS_COLOR[m.permitted_up_to]} styles={NO_TRUNCATE}>
                  {t(`settings.models.class_${m.permitted_up_to}`)}
                </Badge>
              </Table.Td>
              <Table.Td>{t(`settings.models.role_${m.role}`)}</Table.Td>
              <Table.Td><Badge variant="outline" color={m.status === "released" ? "green" : m.status === "blocked" ? "red" : "gray"} styles={NO_TRUNCATE}>{t(`settings.models.status_${m.status}`)}</Badge></Table.Td>
              <Table.Td>{formatNumber(m.cost_in_chf)} / {formatNumber(m.cost_out_chf)}</Table.Td>
              <Table.Td>
                <Group gap={4} justify="flex-end" wrap="nowrap">
                  <Button size="compact-xs" variant="subtle" onClick={() => { setDraft(toBody(m)); setDraftId(m.id); setIsNew(false); }}>{t("doctypes.edit")}</Button>
                  <Button size="compact-xs" variant="subtle" loading={busy === `test:${m.id}`} onClick={() => test(m.id)}>{t("settings.models.test")}</Button>
                  <Button size="compact-xs" variant="subtle" color="red" onClick={() => void run(`del:${m.id}`, () => member.client.deleteModel(m.id).then(() => undefined), t("settings.models.removed"))}>{t("doctypes.remove")}</Button>
                </Group>
                {(() => { const r = tests[m.id]; return r ? <Stack gap={2} align="flex-end"><Text size="xs" c={r.ok ? "green" : "red"} ta="right">{testText(r)}</Text>{r.raw ? <Code block style={{ whiteSpace: "pre-wrap", maxWidth: 420, maxHeight: 160, overflow: "auto", textAlign: "left" }}>{r.raw}</Code> : null}{!r.ok && r.reason === "call_failed" ? <Text size="xs" c="dimmed">{t("settings.models.testLogged")}</Text> : null}</Stack> : null; })()}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>

      {draft && (
        <Card withBorder>
          <Stack gap="sm">
            <Title order={4}>{isNew ? t("settings.models.add") : t("settings.models.editing", { id: draftId })}</Title>
            <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="sm">
              {isNew && <TextInput label="ID" description={t("settings.models.idHelp")} value={draftId} onChange={(e) => setDraftId(e.currentTarget.value.toLowerCase())} />}
              <Select label={t("settings.models.provider")} data={(context?.providers ?? []).map((p) => ({ value: p, label: t(`settings.models.provider_${p}`) }))} value={draft.provider} onChange={(v) => v && setDraft({ ...draft, provider: v as ModelBody["provider"], ...(v === "ollama" ? { local: true, region: "local", clearance: "sensitive" } : { local: false, region: draft.region === "local" ? "US" : draft.region, clearance: draft.clearance === "sensitive" ? "confidential" : draft.clearance }) })} />
              <TextInput label={t("settings.models.model")} description={t("settings.models.modelHelp")} value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.currentTarget.value })} />
              <TextInput label={t("settings.models.baseUrl")} description={t("settings.models.baseUrlHelp")} value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.currentTarget.value })} />
              <Select label={t("settings.models.role")} data={["cheap", "strong"].map((r) => ({ value: r, label: t(`settings.models.role_${r}`) }))} value={draft.role} onChange={(v) => v && setDraft({ ...draft, role: v })} />
              <Select label={t("settings.models.status")} description={t("settings.models.statusHelp")} data={["experimental", "released", "blocked"].map((s) => ({ value: s, label: t(`settings.models.status_${s}`) }))} value={draft.status} onChange={(v) => v && setDraft({ ...draft, status: v })} />
              <Select label={t("settings.models.clearance")} description={t("settings.models.clearanceHelp")} data={CLASSES.filter((c) => draft.provider === "ollama" || c !== "sensitive").map((c) => ({ value: c, label: t(`settings.models.class_${c}`) }))} value={draft.clearance} onChange={(v) => v && setDraft({ ...draft, clearance: v })} />
              {draft.provider !== "ollama" && (
                <Select label={t("settings.models.region")} description={t("settings.models.regionHelp")} data={(context?.regions ?? []).filter((r) => r !== "local")} value={draft.region} onChange={(v) => v && setDraft({ ...draft, region: v })} />
              )}
              {draftSecret && (
                <PasswordInput
                  label={t(`settings.models.key_${draftSecret}`)}
                  description={draftSecretStatus === "none" ? t("settings.models.keyInFormMissing") : t("settings.models.keyInFormPresent", { from: t(`settings.models.keyFrom_${draftSecretStatus}`) })}
                  placeholder={draftSecretStatus === "none" ? t("settings.models.keyUnset") : "••••••••"}
                  value={draftKey}
                  onChange={(e) => setDraftKey(e.currentTarget.value)}
                  disabled={draftSecretStatus === "env"}
                  autoComplete="off"
                  error={draftSecretStatus === "none" && !draftKey ? t("settings.models.keyMissing") : undefined}
                />
              )}
              <NumberInput label={t("settings.models.costIn")} value={draft.cost_in_chf} min={0} decimalScale={4} onChange={(v) => setDraft({ ...draft, cost_in_chf: Number(v) || 0 })} />
              <NumberInput label={t("settings.models.costOut")} value={draft.cost_out_chf} min={0} decimalScale={4} onChange={(v) => setDraft({ ...draft, cost_out_chf: Number(v) || 0 })} />
            </SimpleGrid>
            <Group>
              {draft.provider !== "ollama" && <Switch label={t("settings.models.dpa")} description={t("settings.models.dpaHelp")} checked={draft.dpa} onChange={(e) => setDraft({ ...draft, dpa: e.currentTarget.checked })} />}
              <Switch label={t("settings.models.vision")} description={t("settings.models.visionHelp")} checked={draft.vision} onChange={(e) => setDraft({ ...draft, vision: e.currentTarget.checked })} />
            </Group>
            <Textarea label={t("settings.models.note")} autosize minRows={1} value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.currentTarget.value })} />
            <Group>
              <Button loading={busy === "save"} disabled={!draftId.trim() || !draft.model.trim()} onClick={save}>{t("app.save")}</Button>
              <Button variant="default" onClick={() => setDraft(null)}>{t("app.cancel")}</Button>
            </Group>
          </Stack>
        </Card>
      )}
    </Stack>
  );
}
