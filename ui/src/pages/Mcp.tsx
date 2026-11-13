// Verwaltung → MCP-Server (ADR-0061): where an MCP server runs, which key
// opens it (a tool secret, set here, never shown again) and which of its
// tools may be called - the allowlist. Solutions only name the server.
import { Alert, Badge, Button, Card, Checkbox, Group, NumberInput, PasswordInput, Select, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import type { McpServerView, McpToolInfo, SolutionRow } from "../api/client";
import { useI18n } from "../i18n";
import { useRecordMember } from "./Records";

interface Draft {
  name: string;
  url: string;
  headerName: string;
  headerPrefix: string;
  secretName: string;
  secretValue: string;
  tools: string[];
  timeout: number;
  note: string;
  isNew: boolean;
}

const emptyDraft = (): Draft => ({
  name: "", url: "", headerName: "Authorization", headerPrefix: "Bearer ", secretName: "", secretValue: "",
  tools: [], timeout: 60, note: "", isNew: true,
});

/** Ready-made entries for hosted MCP servers - the key stays the user's. */
const PRESETS = {
  zapier: () => ({
    ...emptyDraft(),
    name: "zapier",
    url: "https://mcp.zapier.com/api/v1/connect",
    secretName: "zapier_mcp_token",
    timeout: 120,
    note: "Zapier MCP – Aktionen auf mcp.zapier.com freigeben; 2 Tasks je Aufruf",
  }),
};

function fromServer(s: McpServerView): Draft {
  const [headerName, value] = Object.entries(s.headers)[0] ?? ["Authorization", ""];
  const m = /^(.*)\{secret:([a-z0-9_]+)\}$/.exec(value);
  return {
    name: s.name, url: s.url, headerName, headerPrefix: m?.[1] ?? "Bearer ", secretName: m?.[2] ?? "",
    secretValue: "", tools: s.tools, timeout: s.timeout_seconds, note: s.note, isNew: false,
  };
}

export function McpPage() {
  const { t } = useI18n();
  const member = useRecordMember();
  const [servers, setServers] = useState<McpServerView[]>([]);
  const [stdioAllowed, setStdioAllowed] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [tools, setTools] = useState<{ server: string; list: McpToolInfo[]; allowed: string[] } | null>(null);
  const [solutions, setSolutions] = useState<SolutionRow[]>([]);
  const [target, setTarget] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!member) return;
    member.client.mcpServers()
      .then((r) => {
        setServers(r.servers);
        setStdioAllowed(r.stdio_allowed);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
    member.client.solutions().then((rows) => setSolutions(rows.filter((r) => r.writable))).catch(() => undefined);
  }, [member]);
  useEffect(load, [load]);

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setError(null);
    setMessage(null);
    try {
      await fn();
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const save = () => run("save", async () => {
    if (!member || !draft) return;
    if (draft.secretName && draft.secretValue) await member.client.setSecret(draft.secretName, draft.secretValue);
    const headers = draft.secretName ? { [draft.headerName || "Authorization"]: `${draft.headerPrefix}{secret:${draft.secretName}}` } : {};
    await member.client.putMcpServer(draft.name, { url: draft.url, headers, tools: draft.tools, timeout_seconds: draft.timeout, note: draft.note });
    setMessage(t("settings.mcp.saved", { name: draft.name }));
    setDraft(null);
  });

  const fetchTools = (name: string) => run(`tools:${name}`, async () => {
    if (!member) return;
    const r = await member.client.mcpTools(name);
    setTools({ server: name, list: r.tools, allowed: r.tools.filter((x) => x.allowed).map((x) => x.name) });
  });

  const saveAllowlist = () => run("allow", async () => {
    if (!member || !tools) return;
    const s = servers.find((x) => x.name === tools.server);
    if (!s) return;
    await member.client.putMcpServer(s.name, {
      url: s.url, headers: s.headers, command: s.command.length ? s.command : undefined, env: s.env,
      tools: tools.allowed, timeout_seconds: s.timeout_seconds, note: s.note,
    });
    setMessage(t("settings.mcp.saved", { name: s.name }));
  });

  const asTool = (tool: string) => run(`make:${tool}`, async () => {
    if (!member || !tools || !target) return;
    const out = await member.client.toolFromMcp(target, tools.server, tool);
    setMessage([t("settings.mcp.created", { id: out.id, app: target }), ...out.warnings].join(" – "));
  });

  if (!member) return null;
  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Title order={2}>{t("settings.mcp.title")}</Title>
        <Group gap="xs">
          <Button variant="light" onClick={() => setDraft(PRESETS.zapier())}>{t("settings.mcp.presetZapier")}</Button>
          <Button onClick={() => setDraft(emptyDraft())}>{t("settings.mcp.add")}</Button>
        </Group>
      </Group>
      <Text c="dimmed">{t("settings.mcp.intro")}</Text>
      <Text size="sm">{t("settings.mcp.stdio", { state: stdioAllowed ? t("settings.mcp.stdioOn") : t("settings.mcp.stdioOff") })}</Text>
      {error && <Alert color="red" withCloseButton onClose={() => setError(null)}>{error}</Alert>}
      {message && <Alert color="teal" withCloseButton onClose={() => setMessage(null)}>{message}</Alert>}

      {draft && (
        <Card withBorder>
          <Stack gap="xs">
            <Group grow>
              <TextInput label={t("settings.mcp.name")} description={t("settings.mcp.nameHelp")} value={draft.name} disabled={!draft.isNew}
                onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value.toLowerCase() })} />
              <TextInput label={t("settings.mcp.url")} placeholder="https://…/mcp" value={draft.url}
                onChange={(e) => setDraft({ ...draft, url: e.currentTarget.value.trim() })} />
            </Group>
            <Group grow>
              <TextInput label={t("settings.mcp.headerName")} value={draft.headerName} onChange={(e) => setDraft({ ...draft, headerName: e.currentTarget.value })} />
              <TextInput label={t("settings.mcp.headerPrefix")} value={draft.headerPrefix} onChange={(e) => setDraft({ ...draft, headerPrefix: e.currentTarget.value })} />
              <TextInput label={t("settings.mcp.secretName")} placeholder="github_token" value={draft.secretName}
                onChange={(e) => setDraft({ ...draft, secretName: e.currentTarget.value.toLowerCase().replace(/[^a-z0-9_]/g, "_") })} />
              <PasswordInput label={t("settings.mcp.secretValue")} value={draft.secretValue} onChange={(e) => setDraft({ ...draft, secretValue: e.currentTarget.value })} />
            </Group>
            {draft.isNew && draft.name === "zapier" && (
              <Alert color="blue">{t("settings.mcp.zapierHelp")}</Alert>
            )}
            <Group grow>
              <NumberInput label={t("settings.mcp.timeout")} min={1} max={600} value={draft.timeout} onChange={(v) => setDraft({ ...draft, timeout: Number(v) || 60 })} />
              <TextInput label={t("settings.mcp.note")} value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.currentTarget.value })} />
            </Group>
            <Group>
              <Button loading={busy === "save"} disabled={!draft.name || !draft.url} onClick={() => void save()}>{t("settings.mcp.save")}</Button>
              <Button variant="subtle" onClick={() => setDraft(null)}>×</Button>
            </Group>
          </Stack>
        </Card>
      )}

      {servers.length === 0 ? (
        <Text c="dimmed">{t("settings.mcp.noServers")}</Text>
      ) : (
        <Table>
          <Table.Tbody>
            {servers.map((s) => (
              <Table.Tr key={s.name}>
                <Table.Td><Text fw={600}>{s.name}</Text><Text size="xs" c="dimmed">{s.url || s.command.join(" ")}</Text></Table.Td>
                <Table.Td>
                  <Text size="sm">{t("settings.mcp.allowlist", { n: s.tools.length ? s.tools.join(", ") : t("settings.mcp.none") })}</Text>
                  {s.missing_secrets.length > 0 && <Badge color="orange" variant="light">{t("settings.mcp.missing", { names: s.missing_secrets.join(", ") })}</Badge>}
                </Table.Td>
                <Table.Td>
                  <Group gap="xs" justify="flex-end">
                    <Button size="xs" variant="light" loading={busy === `tools:${s.name}`} onClick={() => void fetchTools(s.name)}>{t("settings.mcp.fetchTools")}</Button>
                    <Button size="xs" variant="default" onClick={() => setDraft(fromServer(s))}>{t("settings.mcp.edit")}</Button>
                    <Button size="xs" variant="subtle" color="red" onClick={() => void run(`del:${s.name}`, async () => {
                      await member.client.deleteMcpServer(s.name);
                      setMessage(t("settings.mcp.removed", { name: s.name }));
                      if (tools?.server === s.name) setTools(null);
                    })}>{t("settings.mcp.remove")}</Button>
                  </Group>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}

      {tools && (
        <Card withBorder>
          <Group justify="space-between" mb="xs">
            <Title order={4}>{t("settings.mcp.tools", { name: tools.server })}</Title>
            <Group gap="xs">
              <Select size="xs" placeholder={t("settings.mcp.asTool")} data={solutions.map((s) => s.id)} value={target} onChange={setTarget} />
              <Button size="xs" loading={busy === "allow"} onClick={() => void saveAllowlist()}>{t("settings.mcp.saveAllowlist")}</Button>
            </Group>
          </Group>
          <Table fz="sm">
            <Table.Tbody>
              {tools.list.map((tool) => (
                <Table.Tr key={tool.name}>
                  <Table.Td style={{ width: "1%" }}>
                    <Checkbox label={t("settings.mcp.allowed")} checked={tools.allowed.includes(tool.name)}
                      onChange={(e) => {
                        const on = e.currentTarget.checked;
                        setTools({ ...tools, allowed: on ? [...tools.allowed, tool.name] : tools.allowed.filter((x) => x !== tool.name) });
                      }} />
                  </Table.Td>
                  <Table.Td>
                    <Text fw={600} size="sm">{tool.title || tool.name}</Text>
                    <Text size="xs" c="dimmed">{tool.description}</Text>
                    <Group gap={4} mt={2}>
                      {tool.annotations.destructiveHint && <Badge size="xs" color="red" variant="light">{t("settings.mcp.destructive")}</Badge>}
                      {tool.annotations.readOnlyHint && <Badge size="xs" color="gray" variant="light">{t("settings.mcp.readOnly")}</Badge>}
                      {Object.keys(tool.input_schema.properties ?? {}).map((p) => <Badge key={p} size="xs" variant="outline">{p}</Badge>)}
                    </Group>
                  </Table.Td>
                  <Table.Td style={{ width: "1%" }}>
                    <Button size="xs" variant="light" disabled={!target} loading={busy === `make:${tool.name}`} onClick={() => void asTool(tool.name)}>
                      {t("settings.mcp.asTool")} {target ?? "…"}
                    </Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Card>
      )}
    </Stack>
  );
}
