// Wissen → Wissensräume (ADR-0031): the shelves an instance's agents may
// rely on. A space carries a data class and an owner; documents are
// versioned files, indexed locally; a search box shows what a step would
// get - with the source of every passage. Nothing here leaves the house.
// Web sources (ADR-0045): a space may also keep itself up to date from a
// website - crawled on this instance, at an interval, by document type.
import { Alert, Anchor, Badge, Button, Card, Chip, Code, Collapse, FileButton, Group, Modal, NumberInput, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput, Textarea, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { KNOWLEDGE_DOC_TYPES, type InstanceClient, type KnowledgeDocument, type KnowledgeHit, type KnowledgeSource, type KnowledgeSourceInput, type KnowledgeSpace, type Multilingual } from "../api/client";
import { useAcrossHousehold, useHousehold } from "../household";
import { useI18n } from "../i18n";

const CLASSES = ["public", "internal", "confidential", "sensitive"];
const CLASS_COLOR: Record<string, string> = { public: "gray", internal: "blue", confidential: "orange", sensitive: "red" };

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function SpaceForm({ client, existing, onDone, onCancel }: { client: InstanceClient; existing?: KnowledgeSpace | null; onDone: () => void; onCancel: () => void }) {
  const { t, pick, lang } = useI18n();
  const [id, setId] = useState(existing?.id ?? "");
  const [title, setTitle] = useState(existing ? pick(existing.title) : "");
  const [description, setDescription] = useState(existing ? pick(existing.description) || "" : "");
  const [dataClass, setDataClass] = useState(existing?.data_class ?? "internal");
  const [owner, setOwner] = useState(existing?.owner ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      // Editing changes the text of the UI language and keeps the others
      // (a bundle-declared space arrives in four languages); a new space
      // starts in the lead language.
      const merge = (base: Multilingual | undefined, value: string): Multilingual => {
        if (!value) return "";
        if (base && typeof base === "object") return { de: base.de ?? value, ...base, [lang]: value };
        return lang === "de" ? value : { de: value, [lang]: value };
      };
      await client.saveKnowledgeSpace(id.trim(), {
        title: merge(existing?.title, title.trim()),
        description: description.trim() ? merge(existing?.description, description.trim()) : null,
        data_class: dataClass,
        owner: owner.trim(),
      });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="xs">
      {!existing && <TextInput label={t("knowledge.id")} description={t("knowledge.idHelp")} value={id} onChange={(e) => setId(e.currentTarget.value)} />}
      <TextInput label={t("knowledge.spaceTitle")} value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
      <Textarea label={t("knowledge.description")} autosize minRows={2} value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
      <Group grow>
        <Select label={t("apps.dataClass")} description={t("knowledge.dataClassHelp")} data={CLASSES.map((c) => ({ value: c, label: t(`settings.models.class_${c}`) }))} value={dataClass} onChange={(v) => v && setDataClass(v)} />
        <TextInput label={t("knowledge.owner")} description={t("knowledge.ownerHelp")} value={owner} onChange={(e) => setOwner(e.currentTarget.value)} />
      </Group>
      {error && <Alert color="red">{error}</Alert>}
      <Group>
        <Button loading={busy} disabled={!id.trim() || !title.trim()} onClick={() => void save()}>{t("app.save")}</Button>
        <Button variant="subtle" onClick={onCancel}>{t("app.cancel")}</Button>
      </Group>
    </Stack>
  );
}

const INTERVALS = [0, 6, 12, 24, 168, 720];
const STATUS_COLOR: Record<string, string> = { ok: "teal", partial: "yellow", failed: "red", running: "blue" };
// Running, or due for its first run - the list polls until it settles.
const pending = (s: KnowledgeSource) => s.running || s.last_status === "running" || (s.enabled && s.next_run_at !== null && !s.last_run_at);
const NEW_SOURCE: KnowledgeSourceInput = { url: "", title: "", scope: "path", doc_types: ["html", "pdf"], max_pages: 200, max_depth: 3, exclude: [], interval_hours: 168, enabled: true };

function SourceForm({ client, space, existing, onDone, onCancel }: { client: InstanceClient; space: string; existing: KnowledgeSource | null; onDone: () => void; onCancel: () => void }) {
  const { t } = useI18n();
  const [form, setForm] = useState<KnowledgeSourceInput>(() => (existing ? { url: existing.url, title: existing.title, scope: existing.scope, doc_types: existing.doc_types, max_pages: existing.max_pages, max_depth: existing.max_depth, exclude: existing.exclude, interval_hours: existing.interval_hours, enabled: existing.enabled } : NEW_SOURCE));
  const [exclude, setExclude] = useState((existing?.exclude ?? []).join("\n"));
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof KnowledgeSourceInput>(key: K, value: KnowledgeSourceInput[K]) => setForm((f) => ({ ...f, [key]: value }));
  const intervals = INTERVALS.includes(form.interval_hours) ? INTERVALS : [...INTERVALS, form.interval_hours].sort((a, b) => a - b);
  const intervalLabel = (h: number) => (INTERVALS.includes(h) ? t(`knowledge.interval_${h}`) : t("knowledge.intervalHours", { n: h }));
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = { ...form, url: form.url.trim(), title: form.title.trim(), exclude: exclude.split("\n").map((l) => l.trim()).filter(Boolean) };
      if (existing) await client.saveKnowledgeSource(space, existing.id, body);
      else await client.addKnowledgeSource(space, body);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="xs">
      <TextInput label={t("knowledge.sourceUrl")} description={t("knowledge.sourceUrlHelp")} placeholder="https://" value={form.url} onChange={(e) => set("url", e.currentTarget.value)} />
      <TextInput label={t("knowledge.sourceTitle")} value={form.title} onChange={(e) => set("title", e.currentTarget.value)} />
      <Select label={t("knowledge.scope")} description={t("knowledge.scopeHelp")} data={(["page", "path", "site"] as const).map((v) => ({ value: v, label: t(`knowledge.scope_${v}`) }))} value={form.scope} onChange={(v) => v && set("scope", v as KnowledgeSourceInput["scope"])} />
      <div>
        <Text size="sm" fw={500}>{t("knowledge.docTypes")}</Text>
        <Text size="xs" c="dimmed" mb={6}>{t("knowledge.docTypesHelp")}</Text>
        <Chip.Group multiple value={form.doc_types} onChange={(v) => set("doc_types", v)}>
          <Group gap={6}>
            {KNOWLEDGE_DOC_TYPES.map((kind) => (
              <Chip key={kind} value={kind} size="xs">{t(`knowledge.type_${kind}`)}</Chip>
            ))}
          </Group>
        </Chip.Group>
      </div>
      <Group grow align="flex-end">
        <Select label={t("knowledge.interval")} data={intervals.map((h) => ({ value: String(h), label: intervalLabel(h) }))} value={String(form.interval_hours)} onChange={(v) => v !== null && set("interval_hours", Number(v))} />
        <Switch label={t("knowledge.enabled")} checked={form.enabled} onChange={(e) => set("enabled", e.currentTarget.checked)} mb={8} />
      </Group>
      <Anchor size="xs" component="button" type="button" onClick={() => setAdvanced((a) => !a)}>{t("knowledge.advanced")}</Anchor>
      <Collapse expanded={advanced}>
        <Stack gap="xs">
          <Group grow align="flex-end">
            <NumberInput label={t("knowledge.maxPages")} min={1} max={2000} value={form.max_pages} onChange={(v) => set("max_pages", Number(v) || 1)} />
            <NumberInput label={t("knowledge.maxDepth")} description={t("knowledge.maxDepthHelp")} min={0} max={10} value={form.max_depth} onChange={(v) => set("max_depth", Number(v) || 0)} />
          </Group>
          <Textarea label={t("knowledge.exclude")} description={t("knowledge.excludeHelp")} autosize minRows={2} value={exclude} onChange={(e) => setExclude(e.currentTarget.value)} />
        </Stack>
      </Collapse>
      {error && <Alert color="red">{error}</Alert>}
      <Group>
        <Button loading={busy} disabled={!form.url.trim() || !form.doc_types.length} onClick={() => void save()}>{t("app.save")}</Button>
        <Button variant="subtle" onClick={onCancel}>{t("app.cancel")}</Button>
      </Group>
    </Stack>
  );
}

function SourcesCard({ client, space, onCrawled }: { client: InstanceClient; space: string; onCrawled: () => void }) {
  const { t, formatDate } = useI18n();
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [editing, setEditing] = useState<KnowledgeSource | "new" | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wasRunning = useRef(false);

  const load = async () => {
    try {
      const rows = await client.knowledgeSources(space);
      setSources(rows);
      const running = rows.some(pending);
      // A crawl just ended: the document list below has news.
      if (wasRunning.current && !running) onCrawled();
      wasRunning.current = running;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [space]);
  const anyRunning = sources.some(pending);
  useEffect(() => {
    if (!anyRunning) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anyRunning, space]);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };
  const intervalLabel = (h: number) => (INTERVALS.includes(h) ? t(`knowledge.interval_${h}`) : t("knowledge.intervalHours", { n: h }));

  return (
    <Card withBorder>
      <Group justify="space-between">
        <Title order={5}>{t("knowledge.sources")}</Title>
        <Button size="xs" variant="light" onClick={() => setEditing("new")}>{t("knowledge.addSource")}</Button>
      </Group>
      <Text size="xs" c="dimmed" mt={4}>{t("knowledge.sourcesHelp")}</Text>
      {error && <Alert color="red" mt="sm">{error}</Alert>}
      {!sources.length && <Text c="dimmed" size="sm" mt="sm">{t("knowledge.noSources")}</Text>}
      {sources.length > 0 && (
        <Table mt="sm" verticalSpacing="xs">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("knowledge.sourceUrl")}</Table.Th>
              <Table.Th>{t("knowledge.docTypes")}</Table.Th>
              <Table.Th>{t("knowledge.interval")}</Table.Th>
              <Table.Th>{t("knowledge.lastRun")}</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {sources.map((s) => {
              const status = s.running ? "running" : s.last_status;
              return (
                <Table.Tr key={s.id} style={{ opacity: s.enabled ? 1 : 0.6 }}>
                  <Table.Td>
                    {s.title && <Text size="sm" fw={600}>{s.title}</Text>}
                    <Anchor size="sm" href={s.url} target="_blank" rel="noopener noreferrer">{s.url}</Anchor>
                    <Text size="xs" c="dimmed">{t(`knowledge.scope_${s.scope}`)} · {t("knowledge.sourceDocs", { n: s.documents })}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4}>
                      {s.doc_types.map((kind) => <Badge key={kind} size="xs" variant="outline" color="gray">{t(`knowledge.type_${kind}`)}</Badge>)}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{intervalLabel(s.interval_hours)}</Text>
                    {s.enabled && s.next_run_at && <Text size="xs" c="dimmed">{t("knowledge.nextRun")}: {formatDate(s.next_run_at)}</Text>}
                  </Table.Td>
                  <Table.Td>
                    {status ? <Badge size="sm" variant="light" color={STATUS_COLOR[status] ?? "gray"}>{t(`knowledge.status_${status}`)}</Badge> : <Text size="xs" c="dimmed">{t("knowledge.never")}</Text>}
                    {s.last_run_at && <Text size="xs" c="dimmed">{formatDate(s.last_run_at)}</Text>}
                    {s.last_stats && status !== "running" && <Text size="xs" c="dimmed">{t("knowledge.stats", { found: s.last_stats.found, new: s.last_stats.new, updated: s.last_stats.updated, removed: s.last_stats.removed })}</Text>}
                    {s.last_error && status !== "running" && <Text size="xs" c={status === "failed" ? "red" : "dimmed"} lineClamp={2}>{s.last_error}</Text>}
                  </Table.Td>
                  <Table.Td>
                    <Group gap="xs" justify="flex-end" wrap="nowrap">
                      <Button size="compact-xs" variant="light" loading={busy === `run:${s.id}` || status === "running"} disabled={status === "running"} onClick={() => void act(`run:${s.id}`, () => client.runKnowledgeSource(space, s.id))}>{status === "running" ? t("knowledge.running") : t("knowledge.runNow")}</Button>
                      <Button size="compact-xs" variant="subtle" onClick={() => setEditing(s)}>{t("knowledge.edit")}</Button>
                      {confirm === s.id ? (
                        <Button size="compact-xs" color="red" loading={busy === `del:${s.id}`} onClick={() => void act(`del:${s.id}`, async () => { await client.deleteKnowledgeSource(space, s.id); setConfirm(null); onCrawled(); })}>{t("knowledge.removeSourceConfirm")}</Button>
                      ) : (
                        <Button size="compact-xs" variant="subtle" color="red" onClick={() => setConfirm(s.id)}>{t("knowledge.removeSource")}</Button>
                      )}
                    </Group>
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      )}
      <Modal opened={editing !== null} onClose={() => setEditing(null)} title={editing === "new" ? t("knowledge.addSource") : t("knowledge.editSource")} size="lg">
        {editing !== null && (
          <SourceForm client={client} space={space} existing={editing === "new" ? null : editing} onDone={() => { setEditing(null); void load(); }} onCancel={() => setEditing(null)} />
        )}
      </Modal>
    </Card>
  );
}

function SpaceDetail({ client, space, onChanged, onClose }: { client: InstanceClient; space: KnowledgeSpace; onChanged: () => void; onClose: () => void }) {
  const { t, pick, formatDate } = useI18n();
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);
  const [replaceId, setReplaceId] = useState<string | null>(null);

  const load = async () => {
    try {
      setDocs((await client.knowledgeSpace(space.id)).documents);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [space.id]);

  const upload = async (files: File[]) => {
    setBusy("upload");
    setError(null);
    try {
      for (const file of files) {
        await client.addKnowledgeDocument(space.id, { name: file.name, content_base64: await toBase64(file), replace: replaceId });
      }
      setReplaceId(null);
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };
  const remove = async (doc: KnowledgeDocument) => {
    setBusy(doc.id);
    try {
      await client.deleteKnowledgeDocument(space.id, doc.id);
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };
  const search = async () => {
    setBusy("search");
    try {
      setHits(await client.searchKnowledge(space.id, query));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };
  const archive = async () => {
    setBusy("archive");
    try {
      await client.archiveKnowledgeSpace(space.id);
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(null);
    }
  };

  return (
    <Stack>
      <Group justify="space-between" align="flex-start">
        <div>
          <Group gap="xs">
            <Title order={3}>{pick(space.title)}</Title>
            <Badge variant="light" color={CLASS_COLOR[space.data_class]}>{t(`settings.models.class_${space.data_class}`)}</Badge>
            {space.source !== "local" && <Badge variant="outline" color="gray">{t("knowledge.fromBundle", { app: space.source.replace(/^app:/, "") })}</Badge>}
          </Group>
          <Text size="sm" c="dimmed">{pick(space.description)}</Text>
          <Text size="xs" c="dimmed"><Code>{space.id}</Code>{space.owner && ` · ${t("knowledge.owner")}: ${space.owner}`}</Text>
        </div>
        <Group gap="xs">
          <Button variant="default" size="xs" onClick={() => setEditing(true)}>{t("knowledge.edit")}</Button>
          <Button variant="subtle" size="xs" color="red" loading={busy === "archive"} onClick={() => void archive()}>{t("knowledge.archive")}</Button>
          <Button variant="subtle" size="xs" onClick={onClose}>{t("knowledge.back")}</Button>
        </Group>
      </Group>
      {error && <Alert color="red">{error}</Alert>}
      <Modal opened={editing} onClose={() => setEditing(false)} title={t("knowledge.edit")}>
        <SpaceForm client={client} existing={space} onDone={() => { setEditing(false); onChanged(); }} onCancel={() => setEditing(false)} />
      </Modal>

      <Card withBorder>
        <Group justify="space-between">
          <Title order={5}>{t("knowledge.documents")}</Title>
          <Group gap="xs">
            {replaceId && <Text size="xs" c="dimmed">{t("knowledge.replacing", { title: docs.find((d) => d.id === replaceId)?.title ?? "" })}</Text>}
            <FileButton onChange={(picked) => picked.length && void upload(picked)} multiple accept=".pdf,.docx,.xlsx,.pptx,.odt,.ods,.odp,.html,.htm,.txt,.md,.csv,.png,.jpg,.jpeg,.tif,.tiff,.mp3,.m4a,.wav,.mp4,.webm">
              {(props) => <Button size="xs" loading={busy === "upload"} {...props}>{replaceId ? t("knowledge.uploadVersion") : t("knowledge.upload")}</Button>}
            </FileButton>
            {replaceId && <Button size="xs" variant="subtle" onClick={() => setReplaceId(null)}>{t("app.cancel")}</Button>}
          </Group>
        </Group>
        <Text size="xs" c="dimmed" mt={4}>{t("knowledge.uploadHelp")}</Text>
        {!docs.length && <Text c="dimmed" size="sm" mt="sm">{t("knowledge.noDocuments")}</Text>}
        {docs.length > 0 && (
          <Table striped mt="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("knowledge.document")}</Table.Th>
                <Table.Th>{t("knowledge.version")}</Table.Th>
                <Table.Th>{t("knowledge.status")}</Table.Th>
                <Table.Th>{t("knowledge.passages")}</Table.Th>
                <Table.Th>{t("cases.when")}</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {docs.map((d) => (
                <Table.Tr key={d.id}>
                  <Table.Td>
                    <a href={client.knowledgeDocumentUrl(space.id, d.id)} target="_blank" rel="noopener">{d.title}</a>
                    {d.url && <Badge ml={6} size="xs" variant="outline" color="grape">{t("knowledge.web")}</Badge>}
                    {d.url ? (
                      <Text size="xs" c="dimmed" lineClamp={1}><Anchor size="xs" href={d.url} target="_blank" rel="noopener noreferrer">{d.url}</Anchor>{d.method ? ` · ${d.method}` : ""}</Text>
                    ) : (
                      <Text size="xs" c="dimmed">{d.file_name}{d.method ? ` · ${d.method}` : ""}</Text>
                    )}
                  </Table.Td>
                  <Table.Td>{d.version}</Table.Td>
                  <Table.Td>
                    {d.status === "indexed" ? <Badge size="sm" variant="light" color="teal">{t("knowledge.indexed")}</Badge> : <Badge size="sm" variant="light" color="red">{t("knowledge.failed")}</Badge>}
                    {d.error && <Text size="xs" c="red">{d.error}</Text>}
                  </Table.Td>
                  <Table.Td>{d.chunks}{d.embedded && <Text span size="xs" c="dimmed"> · {t("knowledge.embedded")}</Text>}</Table.Td>
                  <Table.Td>{formatDate(d.uploaded_at)}{d.uploaded_by && !d.source_id ? ` · ${d.uploaded_by}` : ""}</Table.Td>
                  <Table.Td>
                    <Group gap="xs" justify="flex-end">
                      {!d.source_id && <Button size="compact-xs" variant="subtle" onClick={() => setReplaceId(d.id)}>{t("knowledge.newVersion")}</Button>}
                      <Button size="compact-xs" variant="subtle" color="red" loading={busy === d.id} onClick={() => void remove(d)}>{t("knowledge.delete")}</Button>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>

      <SourcesCard client={client} space={space.id} onCrawled={() => { void load(); onChanged(); }} />

      <Card withBorder>
        <Title order={5}>{t("knowledge.try")}</Title>
        <Text size="xs" c="dimmed" mt={4}>{t("knowledge.tryHelp")}</Text>
        <Group mt="sm" align="flex-end">
          <TextInput style={{ flex: 1 }} label={t("knowledge.question")} value={query} onChange={(e) => setQuery(e.currentTarget.value)} onKeyDown={(e) => e.key === "Enter" && query.trim() && void search()} />
          <Button loading={busy === "search"} disabled={!query.trim()} onClick={() => void search()}>{t("knowledge.search")}</Button>
        </Group>
        {hits && !hits.length && <Text c="dimmed" size="sm" mt="sm">{t("knowledge.noHits")}</Text>}
        {hits && hits.length > 0 && (
          <Stack gap="xs" mt="sm">
            {hits.map((h, i) => (
              <Card key={i} withBorder padding="sm">
                <Group justify="space-between">
                  <Text size="sm" fw={600}>{h.url ? <Anchor href={h.url} target="_blank" rel="noopener noreferrer" inherit>{h.source}</Anchor> : h.source}</Text>
                  <Text size="xs" c="dimmed">{Math.round(h.score * 100)} %</Text>
                </Group>
                <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{h.text}</Text>
              </Card>
            ))}
          </Stack>
        )}
      </Card>
    </Stack>
  );
}

export function KnowledgePage() {
  const { t, pick } = useI18n();
  const { members } = useHousehold();
  const spaces = useAcrossHousehold<KnowledgeSpace>((c) => c.knowledge().then((r) => r.spaces));
  const [creating, setCreating] = useState<string | null>(null); // member id
  const [open, setOpen] = useState<{ member: string; space: KnowledgeSpace } | null>(null);
  const candidates = members.filter((m) => m.info && m.member.type !== "mailroom");
  const clientOf = (memberId: string) => members.find((m) => m.member.id === memberId)?.client;

  useEffect(() => {
    // Keep the open space fresh after edits.
    if (open) {
      const fresh = spaces.rows.find((s) => s._member.id === open.member && s.id === open.space.id);
      if (fresh && fresh !== open.space) setOpen({ member: open.member, space: fresh });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spaces.rows]);

  if (open) {
    const client = clientOf(open.member);
    if (client) return <SpaceDetail client={client} space={open.space} onChanged={spaces.refresh} onClose={() => setOpen(null)} />;
  }

  return (
    <Stack>
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>{t("knowledge.title")}</Title>
          <Text c="dimmed" size="sm">{t("knowledge.intro")}</Text>
        </div>
        {candidates.length > 0 && !creating && (
          <Button onClick={() => setCreating(candidates.length === 1 ? (candidates[0]?.member.id ?? "") : "")}>{t("knowledge.new")}</Button>
        )}
      </Group>
      {spaces.error && <Alert color="red">{t("app.error", { message: spaces.error })}</Alert>}
      {creating !== null && (
        <Card withBorder>
          <Title order={5}>{t("knowledge.new")}</Title>
          {candidates.length > 1 && (
            <Select mt="xs" label={t("household.members")} data={candidates.map((m) => ({ value: m.member.id, label: m.member.title }))} value={creating || null} onChange={(v) => setCreating(v ?? "")} />
          )}
          {creating && clientOf(creating) && (
            <SpaceForm client={clientOf(creating)!} onDone={() => { setCreating(null); spaces.refresh(); }} onCancel={() => setCreating(null)} />
          )}
        </Card>
      )}
      {!spaces.loading && !spaces.rows.length && <Text c="dimmed">{t("knowledge.empty")}</Text>}
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
        {spaces.rows.map((s) => (
          <Card key={`${s._member.id}:${s.id}`} withBorder style={{ cursor: "pointer" }} onClick={() => setOpen({ member: s._member.id, space: s })}>
            <Group justify="space-between" align="flex-start">
              <Title order={5}>{pick(s.title)}</Title>
              <Badge variant="light" color={CLASS_COLOR[s.data_class]}>{t(`settings.models.class_${s.data_class}`)}</Badge>
            </Group>
            <Text size="sm" c="dimmed" mt={4} lineClamp={2}>{pick(s.description)}</Text>
            <Text size="xs" c="dimmed" mt="xs">
              {s._member.title} · {t("knowledge.count", { documents: s.documents ?? 0, passages: s.chunks ?? 0 })}
              {s.source !== "local" && ` · ${t("knowledge.fromBundle", { app: s.source.replace(/^app:/, "") })}`}
            </Text>
          </Card>
        ))}
      </SimpleGrid>
    </Stack>
  );
}
