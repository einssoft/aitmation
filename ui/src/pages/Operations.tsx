// Verwaltung → Betrieb: the operator's one page - what ran, what runs and
// what it waits on, what is scheduled next, errors of the last day and
// the models' token usage. A view over the trails the instance keeps
// (ADR-0029), per member of the household.
import { Alert, Anchor, Badge, Card, Group, SegmentedControl, SimpleGrid, Stack, Table, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";
import { type Member, type Multilingual, type OperationsOverview } from "../api/client";
import { useAcrossHousehold } from "../household";
import { useI18n } from "../i18n";

const PERIODS = ["1", "7", "30"] as const;
const WAIT_COLOR: Record<string, string> = { gate: "teal", device: "violet", timer: "blue", runtime: "gray" };

function Tile({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <Card withBorder padding="sm">
      <Text size="xl" fw={700} c={color}>{value}</Text>
      <Text size="xs" c="dimmed">{label}</Text>
    </Card>
  );
}

/** Tokens per day as plain bars - no chart library for a dozen numbers. */
function TokenBars({ days }: { days: OperationsOverview["tokens"]["per_day"] }) {
  const { formatNumber } = useI18n();
  const max = Math.max(1, ...days.map((d) => d.tokens_in + d.tokens_out));
  return (
    <Group gap={4} align="flex-end" h={90} wrap="nowrap" style={{ overflowX: "auto" }}>
      {days.map((d) => {
        const total = d.tokens_in + d.tokens_out;
        const height = Math.max(2, Math.round((total / max) * 70));
        return (
          <Stack key={d.day} gap={2} align="center" style={{ minWidth: 26 }} title={`${d.day}: ${formatNumber(total, 0)} tokens, ${formatNumber(d.cost_chf, 2)} CHF, ${d.calls} calls`}>
            <div style={{ width: 18, height, background: d.errors ? "var(--mantine-color-red-5)" : "var(--mantine-color-teal-5)", borderRadius: 3 }} />
            <Text size="9px" c="dimmed">{d.day.slice(5)}</Text>
          </Stack>
        );
      })}
    </Group>
  );
}

function MemberOperations({ member, data }: { member: Member; data: OperationsOverview }) {
  const { t, pick, formatDate, formatNumber } = useI18n();
  const title = (id: string, titles?: Multilingual) => (titles ? pick(titles) : "") || id;
  const win = data.tokens.window;
  return (
    <Stack gap="md">
      <SimpleGrid cols={{ base: 2, sm: 4, lg: 8 }}>
        <Tile label={t("ops.tiles.running")} value={data.cases.running} />
        <Tile label={t("ops.tiles.gate")} value={data.waiting.gate ?? 0} color="teal" />
        <Tile label={t("ops.tiles.device")} value={data.waiting.device ?? 0} color="violet" />
        <Tile label={t("ops.tiles.timer")} value={data.waiting.timer ?? 0} color="blue" />
        <Tile label={t("ops.tiles.completed")} value={data.cases.completed} />
        <Tile label={t("ops.tiles.failed")} value={data.cases.failed} color={data.cases.failed ? "red" : undefined} />
        <Tile label={t("ops.tiles.errors")} value={data.errors_24h.errors} color={data.errors_24h.errors ? "red" : undefined} />
        <Tile label={t("ops.tiles.cost")} value={formatNumber(win.cost_chf, 2)} />
      </SimpleGrid>
      <Text size="xs" c="dimmed">{t("ops.tasks", { open: data.client_tasks.open, claimed: data.client_tasks.claimed })}</Text>

      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Card withBorder>
          <Title order={5}>{t("ops.running")}</Title>
          {data.running.length === 0 ? <Text size="sm" c="dimmed" mt="xs">{t("ops.nothingRunning")}</Text> : (
            <Table verticalSpacing={4} mt="xs">
              <Table.Tbody>
                {data.running.map((r) => (
                  <Table.Tr key={r.case_id}>
                    <Table.Td><Anchor component={Link} to={`/cases/${member.id}/${r.case_id}`} size="sm">{title(r.template_id, r.template_title)}</Anchor></Table.Td>
                    <Table.Td><code style={{ fontSize: 12 }}>{r.step_id ?? "–"}</code></Table.Td>
                    <Table.Td><Badge size="sm" variant="light" color={WAIT_COLOR[r.waiting_on]}>{t(`ops.waitingOn.${r.waiting_on}`)}</Badge></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed">{t("ops.since")} {formatDate(r.since)}</Text></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Card>
        <Card withBorder>
          <Title order={5}>{t("ops.schedules")}</Title>
          {data.schedules.length === 0 ? <Text size="sm" c="dimmed" mt="xs">{t("ops.noSchedules")}</Text> : (
            <Table verticalSpacing={4} mt="xs">
              <Table.Tbody>
                {data.schedules.map((s) => (
                  <Table.Tr key={s.template_id}>
                    <Table.Td><Text size="sm">{(s.title ? pick(s.title) : "") || s.template_id}</Text></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed">{t("ops.next")} {formatDate(s.next_at)}</Text></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Card>
      </SimpleGrid>

      <Card withBorder>
        <Group justify="space-between" align="flex-end">
          <Title order={5}>{t("ops.tokens")}</Title>
          <Text size="xs" c="dimmed">{t("ops.calls")} {win.calls} · {t("ops.in")} {formatNumber(win.tokens_in, 0)} · {t("ops.out")} {formatNumber(win.tokens_out, 0)} · {formatNumber(win.cost_chf, 2)} CHF</Text>
        </Group>
        <Text size="xs" c="dimmed" mt="xs">{t("ops.perDay")}</Text>
        <TokenBars days={data.tokens.per_day} />
        {data.tokens.per_template.length > 0 && (
          <Table verticalSpacing={4} mt="sm" fz="sm">
            <Table.Thead><Table.Tr><Table.Th>{t("ops.perProcess")}</Table.Th><Table.Th>{t("ops.calls")}</Table.Th><Table.Th>{t("ops.cases")}</Table.Th><Table.Th>{t("ops.in")}</Table.Th><Table.Th>{t("ops.out")}</Table.Th><Table.Th>CHF</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.tokens.per_template.map((r) => (
                <Table.Tr key={r.template_id}>
                  <Table.Td>{title(r.template_id, r.template_title)}</Table.Td>
                  <Table.Td>{r.calls}</Table.Td><Table.Td>{r.cases}</Table.Td>
                  <Table.Td>{formatNumber(r.tokens_in, 0)}</Table.Td><Table.Td>{formatNumber(r.tokens_out, 0)}</Table.Td>
                  <Table.Td>{formatNumber(r.cost_chf, 2)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>

      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Card withBorder>
          <Title order={5}>{t("ops.errors")}</Title>
          {data.errors_24h.last.length === 0 ? <Text size="sm" c="dimmed" mt="xs">{t("ops.noErrors")}</Text> : (
            <Stack gap={4} mt="xs">
              {data.errors_24h.last.map((e, i) => (
                <Group key={i} gap="xs" wrap="nowrap" align="flex-start">
                  <Badge size="sm" color={e.level === "error" ? "red" : "orange"} variant="light">{e.source}</Badge>
                  <div style={{ minWidth: 0 }}>
                    <Text size="sm"><code style={{ fontSize: 12 }}>{e.event}</code> {e.detail}</Text>
                    <Text size="xs" c="dimmed">
                      {formatDate(e.at)}
                      {typeof e.context.case_id === "string" && <> · <Anchor component={Link} to={`/cases/${member.id}/${e.context.case_id}`} size="xs">{t("ops.openCase")}</Anchor></>}
                    </Text>
                  </div>
                </Group>
              ))}
            </Stack>
          )}
        </Card>
        <Card withBorder>
          <Title order={5}>{t("ops.recent")}</Title>
          {data.recent.length === 0 ? <Text size="sm" c="dimmed" mt="xs">{t("ops.nothingRecent")}</Text> : (
            <Table verticalSpacing={4} mt="xs" fz="sm">
              <Table.Tbody>
                {data.recent.map((r) => (
                  <Table.Tr key={r.case_id}>
                    <Table.Td><Anchor component={Link} to={`/cases/${member.id}/${r.case_id}`} size="sm">{title(r.template_id, r.template_title)}</Anchor></Table.Td>
                    <Table.Td><Badge size="sm" variant="light" color={r.status === "completed" ? "teal" : "red"}>{t(`cases.status.${r.status}`)}</Badge></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed">{r.last_step}{r.last_outcome ? ` → ${r.last_outcome}` : ""}</Text></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed">{formatDate(r.closed_at)}{r.duration_s != null ? ` · ${Math.round(r.duration_s / 60)} min` : ""}</Text></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Card>
      </SimpleGrid>
    </Stack>
  );
}

export function OperationsPage() {
  const { t } = useI18n();
  const [days, setDays] = useState<(typeof PERIODS)[number]>("7");
  const rows = useAcrossHousehold<OperationsOverview>((c) => c.operations(Number(days)).then((o) => [o]), [days]);
  return (
    <Stack>
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>{t("ops.title")}</Title>
          <Text c="dimmed" size="sm">{t("ops.intro")}</Text>
        </div>
        <SegmentedControl value={days} onChange={(v) => setDays(v as (typeof PERIODS)[number])} data={PERIODS.map((p) => ({ value: p, label: t("ops.period", { n: p }) }))} />
      </Group>
      {rows.error && <Alert color="red">{t("app.error", { message: rows.error })}</Alert>}
      {rows.rows.map((r) => (
        <Stack key={r._member.id} gap="xs">
          {rows.rows.length > 1 && <Badge variant="outline">{r._member.title}</Badge>}
          <MemberOperations member={r._member} data={r} />
        </Stack>
      ))}
    </Stack>
  );
}
