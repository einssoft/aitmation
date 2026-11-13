// What the pipeline produced before a gate (ADR-0041 §4): the steps'
// results as readable fields and the delivered media inline - the
// reviewer sees the caption and the image, not a JSON blob.
import { Alert, Anchor, Badge, Code, Group, Stack, Table, Text } from "@mantine/core";
import { Link } from "react-router-dom";
import type { InstanceClient } from "../api/client";
import { useI18n } from "../i18n";

const HIDE = new Set(["confidence", "model_ref", "fulfilled_by", "file", "bytes"]);
const MAX_ROWS = 20;

/** A list of records (the core sends it as JSON text) becomes a small
 *  table - "the 12 posts to measure", not a blob. */
function asRows(text: string): Record<string, unknown>[] | null {
  if (!text.startsWith("[{")) return null;
  try {
    const parsed: unknown = JSON.parse(text);
    if (Array.isArray(parsed) && parsed.length && parsed.every((r) => r && typeof r === "object" && !Array.isArray(r))) return parsed as Record<string, unknown>[];
  } catch { /* cut off by the limit - show the text */ }
  return null;
}

function RowsTable({ rows }: { rows: Record<string, unknown>[] }) {
  const columns = Array.from(new Set(rows.flatMap((r) => Object.keys(r)))).filter((c) => !["row_id", "saved_at", "case"].includes(c)).slice(0, 6);
  return (
    <Table striped withTableBorder fz="xs" style={{ flex: 1 }}>
      <Table.Thead><Table.Tr>{columns.map((c) => <Table.Th key={c}>{c}</Table.Th>)}</Table.Tr></Table.Thead>
      <Table.Tbody>
        {rows.slice(0, MAX_ROWS).map((r, i) => (
          <Table.Tr key={i}>{columns.map((c) => <Table.Td key={c}>{typeof r[c] === "object" && r[c] !== null ? JSON.stringify(r[c]) : String(r[c] ?? "")}</Table.Td>)}</Table.Tr>
        ))}
        {rows.length > MAX_ROWS && <Table.Tr><Table.Td colSpan={columns.length}>… +{rows.length - MAX_ROWS}</Table.Td></Table.Tr>}
      </Table.Tbody>
    </Table>
  );
}

export function ResultsView({ client, caseId, results, media, stubSteps }: {
  client: InstanceClient;
  caseId: string;
  results?: Record<string, Record<string, unknown>>;
  media?: { name: string; bytes: number; kind: string }[];
  stubSteps?: string[];
}) {
  const { t } = useI18n();
  const steps = Object.entries(results ?? {}).filter(([, fields]) => Object.entries(fields).some(([k, v]) => !HIDE.has(k) && v !== "" && v !== null));
  if (!steps.length && !media?.length && !stubSteps?.length) return null;
  return (
    <Stack gap="xs" mt="sm">
      {stubSteps && stubSteps.length > 0 && (
        <Alert color="orange" variant="light">
          {t("results.stub", { steps: stubSteps.join(", ") })} <Anchor component={Link} to="/settings/models" size="sm">{t("results.stubLink")}</Anchor>
        </Alert>
      )}
      {media?.length ? (
        <Group gap="sm" align="flex-start">
          {media.map((m) => {
            const url = client.mediaUrl(caseId, m.name);
            if (m.kind.startsWith("image/")) return <a key={m.name} href={url} target="_blank" rel="noopener"><img src={url} alt={m.name} style={{ maxWidth: 320, maxHeight: 320, borderRadius: 8, border: "1px solid var(--mantine-color-default-border)" }} /></a>;
            if (m.kind.startsWith("video/")) return <video key={m.name} controls preload="metadata" src={url} style={{ maxWidth: 360, maxHeight: 320, borderRadius: 8 }} />;
            if (m.kind.startsWith("audio/")) return <audio key={m.name} controls preload="none" src={url} />;
            return <a key={m.name} href={url} target="_blank" rel="noopener">{m.name}</a>;
          })}
        </Group>
      ) : null}
      {steps.map(([sid, fields]) => (
        <div key={sid}>
          <Group gap={6} mb={2}><Badge size="xs" variant="outline" color="gray">{sid}</Badge><Text size="xs" c="dimmed">{t("pending.result")}</Text></Group>
          <Stack gap={2}>
            {Object.entries(fields).filter(([k, v]) => !HIDE.has(k) && v !== "" && v !== null && !(Array.isArray(v) && !v.length)).map(([k, v]) => {
              const text = Array.isArray(v) ? v.map(String).join("\n") : typeof v === "object" && v !== null ? JSON.stringify(v) : String(v ?? "");
              const long = text.length > 80 || text.includes("\n");
              const rows = typeof v === "string" ? asRows(v) : null;
              return (
                <Group key={k} gap="xs" align="flex-start" wrap="nowrap">
                  <Text size="xs" c="dimmed" w={110} style={{ flexShrink: 0 }}>{k}</Text>
                  {rows ? <RowsTable rows={rows} /> : long ? <Code block style={{ whiteSpace: "pre-wrap", fontSize: 12, flex: 1 }}>{text}</Code> : <Text size="sm">{text}</Text>}
                </Group>
              );
            })}
          </Stack>
        </div>
      ))}
    </Stack>
  );
}
