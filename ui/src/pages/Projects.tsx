// Werkstatt → Projekte: what was built in this instance.
// A project is a solution of the instance's own - the place where the
// processes that belong together live, with their agents and tools.
// Installed solutions are not projects; they are booked, not built.
import { Alert, Anchor, Badge, Button, Card, Group, SimpleGrid, Stack, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import type { LibraryView, ProjectRow } from "../api/client";
import { useI18n } from "../i18n";
import { useRecordMember } from "./Records";

const message = (err: unknown) => (err instanceof Error ? err.message : String(err));
const slug = (name: string) =>
  name.toLowerCase().replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 50);

export function ProjectsPage() {
  const { t, pick, lang } = useI18n();
  const member = useRecordMember();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [rows, setRows] = useState<ProjectRow[] | null>(null);
  const [view, setView] = useState<LibraryView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ name: string; description: string } | null>(null);
  const [process, setProcess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!member) return;
    member.client.projects().then(setRows).catch((err: unknown) => setError(message(err)));
    member.client.library().then(setView).catch(() => undefined);
  }, [member]);
  useEffect(load, [load]);

  const selected = rows?.find((p) => p.id === params.get("id"));

  const create = async () => {
    if (!member || !draft) return;
    setBusy(true);
    setError(null);
    try {
      const made = await member.client.createProject({ ...draft, lang });
      setDraft(null);
      load();
      setParams({ id: made.id });
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  const addProcess = async () => {
    if (!member || !selected || process === null) return;
    const id = slug(process);
    if (id.length < 2) return;
    setBusy(true);
    setError(null);
    try {
      const made = await member.client.createSolutionElement(selected.id, "process", { id, title: { [lang]: process.trim() }, ai: "", model_role: "default", source: "manual" });
      navigate(`/workshop/solutions?solution=${encodeURIComponent(selected.id)}&kind=process&id=${encodeURIComponent(made.id)}`);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  if (selected) {
    const processes = (view?.processes ?? []).filter((p) => p.solution === selected.id);
    const agents = (view?.agents ?? []).filter((a) => a.solution === selected.id);
    const tools = (view?.tools ?? []).filter((x) => x.solution === selected.id);
    return (
      <Stack>
        <Anchor component="button" onClick={() => setParams({})} size="sm">← {t("nav.projects")}</Anchor>
        <Group justify="space-between">
          <Title order={2}>{pick(selected.name) || selected.id}</Title>
          <Button variant="subtle" component={Link} to={`/workshop/solutions?solution=${encodeURIComponent(selected.id)}`}>{t("projects.explorer")}</Button>
        </Group>
        {pick(selected.description) && <Text c="dimmed">{pick(selected.description)}</Text>}
        {error && <Alert color="red">{error}</Alert>}

        <Group justify="space-between" mt="md">
          <Title order={4}>{t("nav.processes")}</Title>
          <Button variant="light" onClick={() => setProcess("")}>{t("projects.newProcess")}</Button>
        </Group>
        {process !== null && (
          <Card withBorder>
            <Stack>
              <TextInput label={t("library.name")} description={t("projects.processHint")} value={process} onChange={(e) => setProcess(e.currentTarget.value)} autoFocus />
              <Group>
                <Button loading={busy} disabled={slug(process).length < 2} onClick={addProcess}>{t("library.create")}</Button>
                <Button variant="subtle" onClick={() => setProcess(null)}>{t("library.cancel")}</Button>
              </Group>
            </Stack>
          </Card>
        )}
        <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
          {processes.map((p) => (
            <Card key={p.id} withBorder component={Link} to={`/workshop/solutions?solution=${encodeURIComponent(selected.id)}&kind=process&id=${encodeURIComponent(p.id)}`} style={{ textDecoration: "none", color: "inherit" }}>
              <Text fw={600} truncate>{pick(p.title) || p.id}</Text>
              <Group gap="xs" mt="xs">
                <Badge variant="outline" color="gray">{t("library.stepsCount", { n: p.steps })}</Badge>
                <Badge variant="outline" color={p.gates ? "teal" : "gray"}>{t("library.gatesCount", { n: p.gates })}</Badge>
              </Group>
            </Card>
          ))}
        </SimpleGrid>
        {processes.length === 0 && <Text c="dimmed">{t("projects.noProcesses")}</Text>}

        <Group justify="space-between" mt="md">
          <Title order={4}>{t("nav.agents")}</Title>
          <Button variant="light" component={Link} to={`/workshop/agents?project=${encodeURIComponent(selected.id)}`}>{t("library.newAgent")}</Button>
        </Group>
        <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
          {agents.map((a) => (
            <Card key={a.id} withBorder component={Link} to={`/workshop/agents?solution=${encodeURIComponent(selected.id)}&id=${encodeURIComponent(a.id)}`} style={{ textDecoration: "none", color: "inherit" }}>
              <Text fw={600} truncate>{pick(a.name) || a.id}</Text>
              <Text size="sm" c="dimmed" lineClamp={2}>{pick(a.description) || "–"}</Text>
            </Card>
          ))}
        </SimpleGrid>
        {agents.length === 0 && <Text c="dimmed">{t("projects.noAgents")}</Text>}

        <Group justify="space-between" mt="md">
          <Title order={4}>{t("nav.tools")}</Title>
          <Button variant="light" component={Link} to={`/workshop/tools?project=${encodeURIComponent(selected.id)}`}>{t("projects.newTool")}</Button>
        </Group>
        <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
          {tools.map((x) => (
            <Card key={x.id} withBorder>
              <Text fw={600} truncate>{pick(x.name) || x.id}</Text>
              <Text size="sm" c="dimmed" lineClamp={2}>{pick(x.description) || "–"}</Text>
            </Card>
          ))}
        </SimpleGrid>
        {tools.length === 0 && <Text c="dimmed">{t("projects.noTools")}</Text>}
      </Stack>
    );
  }

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("nav.projects")}</Title>
        <Button onClick={() => setDraft({ name: "", description: "" })}>{t("projects.new")}</Button>
      </Group>
      <Text c="dimmed">{t("projects.intro")}</Text>
      {error && <Alert color="red">{error}</Alert>}
      {draft && (
        <Card withBorder>
          <Stack>
            <TextInput label={t("library.name")} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })} autoFocus />
            <TextInput label={t("projects.description")} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.currentTarget.value })} />
            <Group>
              <Button loading={busy} disabled={draft.name.trim().length < 2} onClick={create}>{t("library.create")}</Button>
              <Button variant="subtle" onClick={() => setDraft(null)}>{t("library.cancel")}</Button>
            </Group>
          </Stack>
        </Card>
      )}
      <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
        {(rows ?? []).map((p) => (
          <Card key={p.id} withBorder style={{ cursor: "pointer" }} onClick={() => setParams({ id: p.id })}>
            <Stack gap="xs">
              <Group justify="space-between" wrap="nowrap">
                <Text fw={600} truncate>{pick(p.name) || p.id}</Text>
                {p.own && <Badge color="teal" variant="light">{t("library.own")}</Badge>}
              </Group>
              <Text size="sm" c="dimmed" lineClamp={2}>{pick(p.description) || "–"}</Text>
              <Group gap="xs">
                <Badge variant="outline" color={p.processes.length ? "teal" : "gray"}>{t("projects.processesCount", { n: p.processes.length })}</Badge>
                <Badge variant="outline" color="gray">{t("library.agentsCount", { n: p.agents.length })}</Badge>
                <Badge variant="outline" color="gray">{t("library.toolsCount", { n: p.tools.length })}</Badge>
              </Group>
            </Stack>
          </Card>
        ))}
      </SimpleGrid>
      {rows && rows.length === 0 && <Text c="dimmed">{t("projects.none")}</Text>}
    </Stack>
  );
}
