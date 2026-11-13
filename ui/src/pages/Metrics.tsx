// Verwaltung → Kennzahlen (ADR-0029): what the automation costs (per
// template, per case, per model x task), whether the gates still agree
// with the proposals, how much flows - and the monthly model budget.
// Aggregation over the existing trails; a period switch 7/30/90/all as
// in a metrics tab.
import { Alert, Badge, Button, Card, Code, Group, NumberInput, Progress, SegmentedControl, SimpleGrid, Stack, Switch, Table, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";
import { type BudgetState, type Member, type MetricsOverview, type Multilingual } from "../api/client";
import { useAcrossHousehold, useHousehold } from "../household";
import { useI18n } from "../i18n";

const PERIODS = ["7", "30", "90", "0"] as const;

function BudgetCard({ member, budget, onSaved }: { member: Member; budget: BudgetState; onSaved: () => void }) {
  const { t, formatNumber } = useI18n();
  const { members } = useHousehold();
  const client = members.find((m) => m.member.id === member.id)?.client;
  const [amount, setAmount] = useState<number | string>(budget.monthly_budget_chf);
  const [stop, setStop] = useState(budget.budget_stop);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setAmount(budget.monthly_budget_chf);
    setStop(budget.budget_stop);
  }, [budget.monthly_budget_chf, budget.budget_stop]);
  const color = budget.state === "exceeded" ? "red" : budget.state === "warning" ? "orange" : "teal";
  const save = async () => {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      await client.saveBudget({ monthly_budget_chf: Number(amount) || 0, budget_stop: stop });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card withBorder>
      <Group justify="space-between">
        <Title order={5}>{t("metrics.budget.title")}</Title>
        {budget.state !== "none" && <Badge color={color} variant="light">{t(`metrics.budget.state_${budget.state}`)}</Badge>}
      </Group>
      <Text size="sm" mt="xs">
        {t("metrics.budget.spent", { spent: formatNumber(budget.spent_chf, 2) })}
        {budget.monthly_budget_chf > 0 && ` · ${t("metrics.budget.of", { budget: formatNumber(budget.monthly_budget_chf, 2) })}`}
      </Text>
      {budget.ratio != null && <Progress value={Math.min(100, budget.ratio * 100)} color={color} mt="xs" />}
      {budget.stop && <Alert color="red" mt="sm">{t("metrics.budget.stopped")}</Alert>}
      <Group mt="md" align="flex-end">
        <NumberInput label={t("metrics.budget.monthly")} value={amount} onChange={setAmount} min={0} decimalScale={2} w={180} />
        <Switch label={t("metrics.budget.stop")} checked={stop} onChange={(e) => setStop(e.currentTarget.checked)} mb={6} />
        <Button loading={busy} onClick={() => void save()}>{t("app.save")}</Button>
      </Group>
      <Text size="xs" c="dimmed" mt="xs">{t("metrics.budget.help")}</Text>
      {error && <Alert color="red" mt="sm">{error}</Alert>}
    </Card>
  );
}

function TemplateName({ id, title }: { id: string; title?: Multilingual | null }) {
  const { pick } = useI18n();
  return title ? <>{pick(title)}</> : <Code>{id}</Code>;
}

function MemberFigures({ member, data, onSaved }: { member: Member; data: MetricsOverview; onSaved: () => void }) {
  const { t, formatNumber } = useI18n();
  const chf = (n: number) => `${formatNumber(n, 3)} CHF`;
  return (
    <Stack>
      <Title order={3}>{member.title}</Title>
      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <BudgetCard member={member} budget={data.budget} onSaved={onSaved} />
        <Card withBorder>
          <Title order={5}>{t("metrics.flow.title")}</Title>
          <SimpleGrid cols={4} mt="sm">
            {(["started", "completed", "running", "failed"] as const).map((k) => (
              <div key={k}>
                <Text size="xl" fw={700} c={k === "failed" && data.cases[k] > 0 ? "red" : undefined}>{data.cases[k]}</Text>
                <Text size="xs" c="dimmed">{t(`metrics.flow.${k}`)}</Text>
              </div>
            ))}
          </SimpleGrid>
          <Text size="sm" mt="md">
            {t("metrics.llm.summary", { calls: data.llm.calls, errors: data.llm.errors, cost: formatNumber(data.llm.cost_chf, 2), tin: formatNumber(data.llm.tokens_in, 0), tout: formatNumber(data.llm.tokens_out, 0) })}
          </Text>
        </Card>
      </SimpleGrid>

      <Card withBorder>
        <Title order={5}>{t("metrics.perTemplate.title")}</Title>
        {!data.llm.per_template.length && <Text c="dimmed" size="sm" mt="xs">{t("metrics.empty")}</Text>}
        {data.llm.per_template.length > 0 && (
          <Table striped mt="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("cases.template")}</Table.Th>
                <Table.Th>{t("metrics.calls")}</Table.Th>
                <Table.Th>{t("metrics.errors")}</Table.Th>
                <Table.Th>{t("metrics.duration")}</Table.Th>
                <Table.Th>{t("metrics.cost")}</Table.Th>
                <Table.Th>{t("metrics.perTemplate.perCase")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.llm.per_template.map((r) => (
                <Table.Tr key={r.template_id}>
                  <Table.Td><TemplateName id={r.template_id} title={r.template_title} /></Table.Td>
                  <Table.Td>{r.calls}</Table.Td>
                  <Table.Td c={r.errors ? "red" : undefined}>{r.errors}</Table.Td>
                  <Table.Td>{formatNumber(r.duration_ms, 0)} ms</Table.Td>
                  <Table.Td>{chf(r.cost_chf)}</Table.Td>
                  <Table.Td>
                    {r.cost_per_case_chf != null ? <strong>{chf(r.cost_per_case_chf)}</strong> : "–"}
                    <Text span size="xs" c="dimmed"> ({r.cases})</Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
        <Text size="xs" c="dimmed" mt="xs">{t("metrics.perTemplate.help")}</Text>
      </Card>

      {(data.briefings?.length ?? 0) > 0 && (
        <Card withBorder>
          <Title order={5}>{t("metrics.briefings.title")}</Title>
          <Table striped mt="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("cases.template")}</Table.Th>
                <Table.Th>{t("metrics.briefings.step")}</Table.Th>
                <Table.Th>{t("metrics.briefings.version")}</Table.Th>
                <Table.Th>{t("metrics.briefings.runs")}</Table.Th>
                <Table.Th>{t("metrics.briefings.decisions")}</Table.Th>
                <Table.Th>{t("metrics.briefings.approved")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.briefings!.flatMap((b) =>
                b.versions.map((v, i) => (
                  <Table.Tr key={`${b.template_id}:${b.step}:${v.prompt_hash}`}>
                    <Table.Td>{i === 0 && <TemplateName id={b.template_id} title={b.template_title} />}</Table.Td>
                    <Table.Td>{i === 0 && <Code>{b.step}</Code>}</Table.Td>
                    <Table.Td>
                      {v.prompt_hash ? <Code>{v.prompt_hash.slice(0, 8)}</Code> : <Text span size="sm" c="dimmed">{t("metrics.briefings.unversioned")}</Text>}
                      <Text span size="xs" c="dimmed"> {t("metrics.briefings.since")} {v.first_seen.slice(0, 10)}</Text>
                      {b.current != null && b.current === v.prompt_hash && b.versions.length > 1 && <Text span size="xs" fw={600}> · {t("metrics.briefings.current")}</Text>}
                    </Table.Td>
                    <Table.Td>{v.runs}</Table.Td>
                    <Table.Td>{v.decisions}</Table.Td>
                    <Table.Td c={v.approved_share != null && v.approved_share < 0.6 ? "red" : undefined}>
                      {v.approved_share == null ? "–" : `${formatNumber(v.approved_share * 100, 0)} %`}
                    </Table.Td>
                  </Table.Tr>
                )),
              )}
            </Table.Tbody>
          </Table>
          <Text size="xs" c="dimmed" mt="xs">{t("metrics.briefings.help")}</Text>
        </Card>
      )}

      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <Card withBorder>
          <Title order={5}>{t("metrics.gates.title")}</Title>
          {!data.gates.length && <Text c="dimmed" size="sm" mt="xs">{t("metrics.empty")}</Text>}
          {data.gates.length > 0 && (
            <Table striped mt="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("cases.template")}</Table.Th>
                  <Table.Th>{t("metrics.gates.gate")}</Table.Th>
                  <Table.Th>{t("metrics.gates.decisions")}</Table.Th>
                  <Table.Th>{t("metrics.gates.approved")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.gates.map((g) => (
                  <Table.Tr key={`${g.template_id}:${g.gate}`}>
                    <Table.Td><TemplateName id={g.template_id} title={g.template_title} /></Table.Td>
                    <Table.Td><Code>{g.gate}</Code></Table.Td>
                    <Table.Td>{g.decisions}</Table.Td>
                    <Table.Td c={g.approved_share < 0.6 ? "red" : undefined}>{formatNumber(g.approved_share * 100, 0)} %</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          <Text size="xs" c="dimmed" mt="xs">{t("metrics.gates.help")}</Text>
        </Card>
        <Card withBorder>
          <Title order={5}>{t("metrics.security.title")}</Title>
          {data.security && (
            <Group gap="md" mt="xs">
              <Text size="sm"><b>{data.security.flagged}</b> {t("metrics.security.flagged")}</Text>
              <Text size="sm"><b>{data.security.redacted}</b> {t("metrics.security.redacted")}</Text>
              <Text size="sm" c={data.security.blocked ? "red" : undefined}><b>{data.security.blocked}</b> {t("metrics.security.blocked")}</Text>
              <Text size="sm" c={data.security.refused ? "orange" : undefined}><b>{data.security.refused}</b> {t("metrics.security.refused")}</Text>
            </Group>
          )}
          {data.security && data.security.per_guardrail.length > 0 && (
            <Table striped mt="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("metrics.security.guardrail")}</Table.Th>
                  <Table.Th>{t("metrics.security.action")}</Table.Th>
                  <Table.Th>{t("metrics.security.count")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.security.per_guardrail.map((g) => (
                  <Table.Tr key={`${g.guardrail}:${g.action}`}>
                    <Table.Td><Code>{g.guardrail}</Code></Table.Td>
                    <Table.Td>{g.action}</Table.Td>
                    <Table.Td>{g.count}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          {(!data.security || !data.security.per_guardrail.length) && <Text c="dimmed" size="sm" mt="xs">{t("metrics.security.none")}</Text>}
          <Text size="xs" c="dimmed" mt="xs">{t("metrics.security.help")}</Text>
        </Card>
        <Card withBorder>
          <Title order={5}>{t("metrics.models.title")}</Title>
          {!data.llm.per_model_task.length && <Text c="dimmed" size="sm" mt="xs">{t("metrics.empty")}</Text>}
          {data.llm.per_model_task.length > 0 && (
            <Table striped mt="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t("cases.model")}</Table.Th>
                  <Table.Th>{t("settings.log.task")}</Table.Th>
                  <Table.Th>{t("metrics.calls")}</Table.Th>
                  <Table.Th>{t("settings.log.tokens")}</Table.Th>
                  <Table.Th>{t("metrics.cost")}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.llm.per_model_task.map((r) => (
                  <Table.Tr key={`${r.model_ref}:${r.task}`}>
                    <Table.Td>{r.model_ref}</Table.Td>
                    <Table.Td>{r.task}</Table.Td>
                    <Table.Td>{r.calls}{r.errors ? <Text span c="red" size="xs"> ({r.errors})</Text> : null}</Table.Td>
                    <Table.Td>{formatNumber(r.tokens_in, 0)} / {formatNumber(r.tokens_out, 0)}</Table.Td>
                    <Table.Td>{chf(r.cost_chf)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          {data.llm.per_data_class.length > 0 && (
            <Text size="xs" c="dimmed" mt="xs">
              {t("metrics.models.byClass")}: {data.llm.per_data_class.map((c) => `${c.data_class} ${c.calls}`).join(" · ")}
            </Text>
          )}
        </Card>
      </SimpleGrid>
    </Stack>
  );
}

export function MetricsPage() {
  const { t } = useI18n();
  const [days, setDays] = useState<(typeof PERIODS)[number]>("30");
  const rows = useAcrossHousehold<MetricsOverview>((c) => c.metrics(Number(days)).then((o) => [o]), [days]);
  return (
    <Stack>
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>{t("metrics.title")}</Title>
          <Text c="dimmed" size="sm">{t("metrics.intro")}</Text>
        </div>
        <SegmentedControl
          value={days}
          onChange={(v) => setDays(v as (typeof PERIODS)[number])}
          data={PERIODS.map((p) => ({ value: p, label: p === "0" ? t("metrics.period.all") : t("metrics.period.days", { n: p }) }))}
        />
      </Group>
      {rows.error && <Alert color="red">{t("app.error", { message: rows.error })}</Alert>}
      {rows.rows.map((r) => (
        <MemberFigures key={r._member.id} member={r._member} data={r} onSaved={rows.refresh} />
      ))}
    </Stack>
  );
}
