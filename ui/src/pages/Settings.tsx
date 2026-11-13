// Einstellungen: Protokoll (model calls,
// diagnostics), System (instance facts, household), Regeln (mandates and
// suggestions), and placeholders for what comes later.
import { Alert, Badge, Button, Card, Code, Group, Stack, Table, Tabs, Text, Title } from "@mantine/core";
import { Fragment, useEffect, useState } from "react";
import { type DiagRow, type LlmDetail, type LlmRow, type MandateRow, type Member, type Suggestion } from "../api/client";
import { useAcrossHousehold, useHousehold } from "../household";
import { pick, useI18n, type Text as I18nText } from "../i18n";

const asText = (value: unknown): string =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";

// One model call in full (task text, input, response or raw text, error):
// loaded on demand, since the input may carry the document excerpt.
function LlmDetailRow({ row, colSpan }: { row: LlmRow & { _member: Member }; colSpan: number }) {
  const { t } = useI18n();
  const { members } = useHousehold();
  const [detail, setDetail] = useState<LlmDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const client = members.find((m) => m.member.id === row._member.id)?.client;
  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    client.llmEntry(row.id)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((e: unknown) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [client, row.id]);
  const raw = detail && detail.response && typeof detail.response === "object" && "raw" in (detail.response as object)
    ? (detail.response as { raw?: unknown }).raw
    : undefined;
  return (
    <Table.Tr>
      <Table.Td colSpan={colSpan} style={{ background: "var(--mantine-color-default-hover)" }}>
        {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
        {!detail && !error && <Text size="sm" c="dimmed">{t("app.loading")}</Text>}
        {detail && (
          <Stack gap="xs">
            <Text size="xs" c="dimmed">
              {detail.model_ref ?? "–"} · {detail.duration_ms != null ? `${detail.duration_ms} ms` : "–"}
              {detail.case_id ? ` · ${t("cases.title")} ${detail.case_id.slice(0, 8)}` : ""}
              {detail.step_id ? ` · ${detail.step_id}` : ""}
            </Text>
            <Text size="sm" fw={500}>{t("settings.log.task")}</Text>
            <Code block style={{ whiteSpace: "pre-wrap" }}>{detail.task}</Code>
            <Text size="sm" fw={500}>{t("settings.log.request")}</Text>
            <Code block style={{ whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto" }}>{asText(detail.input)}</Code>
            {detail.error && (
              <>
                <Text size="sm" fw={500} c="red">{t("settings.log.error")}</Text>
                <Code block color="red" style={{ whiteSpace: "pre-wrap" }}>{detail.error}</Code>
              </>
            )}
            {raw !== undefined ? (
              <>
                <Text size="sm" fw={500}>{t("settings.log.rawResponse")}</Text>
                <Code block style={{ whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto" }}>{asText(raw)}</Code>
              </>
            ) : detail.response != null ? (
              <>
                <Text size="sm" fw={500}>{t("settings.log.response")}</Text>
                <Code block style={{ whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto" }}>{asText(detail.response)}</Code>
              </>
            ) : null}
          </Stack>
        )}
      </Table.Td>
    </Table.Tr>
  );
}

export function LogPage() {
  const { t, formatDate, formatNumber } = useI18n();
  const llm = useAcrossHousehold<LlmRow>((c) => c.llmLog());
  const diag = useAcrossHousehold<DiagRow>((c) => c.diagnostics());
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Stack>
      <Title order={2}>{t("settings.log.title")}</Title>
      <Tabs defaultValue="llm">
        <Tabs.List>
          <Tabs.Tab value="llm">{t("settings.log.llm")}</Tabs.Tab>
          <Tabs.Tab value="diag">{t("settings.log.diagnostics")}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="llm" pt="md">
          {!llm.rows.length && <Text c="dimmed">{t("settings.log.empty")}</Text>}
          {llm.rows.length > 0 && (
            <Table striped>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("cases.when")}</Table.Th>
                  <Table.Th>{t("household.members")}</Table.Th>
                  <Table.Th>{t("settings.log.task")}</Table.Th>
                  <Table.Th>{t("cases.model")}</Table.Th>
                  <Table.Th>{t("apps.dataClass")}</Table.Th>
                  <Table.Th>{t("settings.log.tokens")}</Table.Th>
                  <Table.Th>{t("settings.log.cost")}</Table.Th>
                  <Table.Th>{t("settings.log.status")}</Table.Th>
                  <Table.Th />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {llm.rows.map((r) => {
                  const key = `${r._member.id}:${r.id}`;
                  return (
                    <Fragment key={key}>
                      <Table.Tr style={{ cursor: "pointer" }} onClick={() => setOpen(open === key ? null : key)}>
                        <Table.Td>{formatDate(r.at)}</Table.Td>
                        <Table.Td>{r._member.title}</Table.Td>
                        <Table.Td style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.task}</Table.Td>
                        <Table.Td>{r.model_ref ?? "–"}</Table.Td>
                        <Table.Td>{r.data_class ?? "–"}</Table.Td>
                        <Table.Td>{r.tokens_in ?? "–"} / {r.tokens_out ?? "–"}</Table.Td>
                        <Table.Td>{r.cost_chf != null ? formatNumber(r.cost_chf, 4) : "–"}</Table.Td>
                        <Table.Td>
                          <Badge color={r.status === "ok" ? "green" : "red"} variant="light">{r.status}</Badge>
                          {r.error ? <Text size="xs" c="red" style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.error}</Text> : null}
                        </Table.Td>
                        <Table.Td><Text size="xs" c="dimmed">{open === key ? "▲" : "▼"}</Text></Table.Td>
                      </Table.Tr>
                      {open === key && <LlmDetailRow row={r} colSpan={9} />}
                    </Fragment>
                  );
                })}
              </Table.Tbody>
            </Table>
          )}
        </Tabs.Panel>
        <Tabs.Panel value="diag" pt="md">
          {!diag.rows.length && <Text c="dimmed">{t("settings.log.empty")}</Text>}
          {diag.rows.length > 0 && (
            <Table striped>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("cases.when")}</Table.Th>
                  <Table.Th>{t("household.members")}</Table.Th>
                  <Table.Th>{t("settings.log.level")}</Table.Th>
                  <Table.Th>{t("settings.log.source")}</Table.Th>
                  <Table.Th>{t("settings.log.event")}</Table.Th>
                  <Table.Th>{t("settings.log.detail")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {diag.rows.map((r) => (
                  <Table.Tr key={`${r._member.id}:${r.id}`}>
                    <Table.Td>{formatDate(r.at)}</Table.Td>
                    <Table.Td>{r._member.title}</Table.Td>
                    <Table.Td><Badge color={r.level === "error" ? "red" : r.level === "warning" ? "yellow" : "gray"} variant="light">{r.level}</Badge></Table.Td>
                    <Table.Td>{r.source}</Table.Td>
                    <Table.Td>{r.event}</Table.Td>
                    <Table.Td>{r.detail}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

export function SystemPage() {
  const { t } = useI18n();
  const { members } = useHousehold();
  return (
    <Stack>
      <Title order={2}>{t("settings.system.title")}</Title>
      <Group align="stretch" grow>
        {members.map((m) => (
          <Card key={m.member.id} withBorder>
            <Group justify="space-between">
              <Title order={4}>{m.member.title}</Title>
              <Badge variant="light">{t(`household.${m.member.type}`) === `household.${m.member.type}` ? m.member.type : t(`household.${m.member.type}`)}</Badge>
            </Group>
            {m.error && <Alert color="red" mt="sm">{t("household.unreachable", { title: m.member.title })}</Alert>}
            {m.info && (
              <Table mt="sm">
                <Table.Tbody>
                  <Table.Tr><Table.Td>{t("settings.system.version")}</Table.Td><Table.Td>{m.info.version}</Table.Td></Table.Tr>
                  <Table.Tr><Table.Td>{t("settings.system.modelConfigured")}</Table.Td><Table.Td>{t(m.info.model_configured ? "app.yes" : "app.no")}</Table.Td></Table.Tr>
                  <Table.Tr><Table.Td>{t("settings.system.mailConfigured")}</Table.Td><Table.Td>{t(m.info.mail_configured ? "app.yes" : "app.no")}</Table.Td></Table.Tr>
                  <Table.Tr><Table.Td>{t("settings.system.templates")}</Table.Td><Table.Td>{m.info.templates.length}</Table.Td></Table.Tr>
                  <Table.Tr><Table.Td>{t("settings.system.openGates")}</Table.Td><Table.Td>{m.info.open_gates}</Table.Td></Table.Tr>
                  {m.info.capabilities && (
                    <>
                      <Table.Tr><Table.Td>{t("settings.system.ocr")}</Table.Td><Table.Td>{m.info.capabilities.ocr ?? <Text span c="dimmed">{t("settings.system.missing")}</Text>}</Table.Td></Table.Tr>
                      <Table.Tr><Table.Td>{t("settings.system.transcription")}</Table.Td><Table.Td>{m.info.capabilities.transcription ?? <Text span c="dimmed">{t("settings.system.missing")}</Text>}</Table.Td></Table.Tr>
                      <Table.Tr><Table.Td>{t("settings.system.ffmpeg")}</Table.Td><Table.Td>{m.info.capabilities.ffmpeg ? t("app.yes") : <Text span c="dimmed">{t("settings.system.missing")}</Text>}</Table.Td></Table.Tr>
                    </>
                  )}
                </Table.Tbody>
              </Table>
            )}
          </Card>
        ))}
      </Group>
    </Stack>
  );
}

export function RulesPage() {
  const { t, lang, formatDate } = useI18n();
  const { members } = useHousehold();
  const mandates = useAcrossHousehold<MandateRow>((c) => c.mandates());
  const suggestions = useAcrossHousehold<Suggestion>((c) => c.suggestions());
  const clientOf = (id: string) => members.find((m) => m.member.id === id)?.client;
  const grant = async (s: Suggestion & { _member: { id: string } }) => {
    await clientOf(s._member.id)?.grant({ action: s.action, target: s.target, condition: s.condition, comment: s.reason });
    mandates.refresh();
    suggestions.refresh();
  };
  const revoke = async (m: MandateRow & { _member: { id: string } }) => {
    await clientOf(m._member.id)?.revoke(m.id);
    mandates.refresh();
    suggestions.refresh();
  };
  return (
    <Stack>
      <Title order={2}>{t("settings.mandates.title")}</Title>
      <Text c="dimmed" size="sm">{t("settings.mandates.intro")}</Text>
      {suggestions.rows.length > 0 && (
        <Card withBorder>
          <Title order={5}>{t("settings.mandates.suggestions")}</Title>
          <Stack mt="sm">
            {suggestions.rows.map((s, i) => (
              <Group key={i} justify="space-between">
                <Text size="sm">
                  {s.action.startsWith("gate:")
                    ? t("settings.mandates.suggestGate", { count: s.count, target: s.target, process: pick(s.title as I18nText, lang) || s.template_id || "" })
                    : t("settings.mandates.suggest", { count: s.count, target: s.target })}{" "}
                  <Code>{JSON.stringify(s.condition)}</Code>
                </Text>
                <Button size="xs" onClick={() => void grant(s)}>{t("settings.mandates.grant")}</Button>
              </Group>
            ))}
          </Stack>
        </Card>
      )}
      <Title order={5}>{t("settings.mandates.active")}</Title>
      {!mandates.rows.length && <Text c="dimmed">{t("settings.mandates.none")}</Text>}
      {mandates.rows.length > 0 && (
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("household.members")}</Table.Th>
              <Table.Th>{t("settings.mandates.target")}</Table.Th>
              <Table.Th>{t("settings.mandates.condition")}</Table.Th>
              <Table.Th>{t("settings.mandates.granted")}</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {mandates.rows.map((m) => (
              <Table.Tr key={`${m._member.id}:${m.id}`}>
                <Table.Td>{m._member.title}</Table.Td>
                <Table.Td>{m.action} → {m.target}</Table.Td>
                <Table.Td><Code>{Object.keys(m.condition).length ? JSON.stringify(m.condition) : "*"}</Code></Table.Td>
                <Table.Td>{formatDate(m.granted_at)} · {m.granted_by}</Table.Td>
                <Table.Td>
                  {m.revoked_at ? (
                    <Badge color="gray" variant="light">{t("settings.mandates.revoked")}</Badge>
                  ) : (
                    <Button size="xs" variant="default" onClick={() => void revoke(m)}>{t("settings.mandates.revoke")}</Button>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

export function Placeholder({ titleKey, textKey }: { titleKey: string; textKey: string }) {
  const { t } = useI18n();
  return (
    <Stack>
      <Title order={2}>{t(titleKey)}</Title>
      <Text c="dimmed">{t(textKey)}</Text>
    </Stack>
  );
}
