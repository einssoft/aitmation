// Vorgänge: running and closed cases across the household, and one case
// with its steps and the audit trail — the trail is a product feature
// (ADR-0021 §5), not an admin log.
import { Alert, Anchor, Badge, Code, Group, SegmentedControl, Stack, Table, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { type CaseDetail, type CaseRow } from "../api/client";
import { useAcrossHousehold, useHousehold } from "../household";
import { useI18n } from "../i18n";
import { DocumentView } from "../components/DocumentView";
import { ResultsView } from "../components/ResultsView";
import { FollowUpForm } from "./Deadlines";

const STATUS_COLOR: Record<string, string> = { running: "blue", completed: "green", failed: "red", cancelled: "gray" };

export function CasesPage() {
  const { t, formatDate, pick } = useI18n();
  const [filter, setFilter] = useState<string>("all");
  const { rows, loading, error } = useAcrossHousehold<CaseRow>((c) => c.cases(filter === "all" ? undefined : filter), [filter]);
  const sorted = [...rows].sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>{t("cases.title")}</Title>
        <SegmentedControl
          size="xs"
          value={filter}
          onChange={setFilter}
          data={["all", "running", "completed", "failed"].map((v) => ({ value: v, label: t(`cases.filter.${v}`) }))}
        />
      </Group>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {loading && !rows.length && <Text c="dimmed">{t("app.loading")}</Text>}
      {!loading && !rows.length && <Text c="dimmed">{t("cases.empty")}</Text>}
      {sorted.length > 0 && (
        <Table striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("cases.template")}</Table.Th>
              <Table.Th>{t("pending.document")}</Table.Th>
              <Table.Th>{t("household.members")}</Table.Th>
              <Table.Th>{t("cases.currentStep")}</Table.Th>
              <Table.Th>{t("cases.updated")}</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {sorted.map((c) => (
              <Table.Tr key={`${c._member.id}:${c.id}`}>
                <Table.Td>
                  <Anchor component={Link} to={`/cases/${c._member.id}/${c.id}`}>
                    {pick(c.template_titles) || c.template_title}
                  </Anchor>
                </Table.Td>
                <Table.Td>{c.document?.original_name ?? c.document?.subject ?? "–"}</Table.Td>
                <Table.Td>{c._member.title}</Table.Td>
                <Table.Td>{c.current_step_id ? <Code>{c.current_step_id}</Code> : "–"}</Table.Td>
                <Table.Td>{formatDate(c.updated_at)}</Table.Td>
                <Table.Td>
                  <Badge color={STATUS_COLOR[c.status] ?? "gray"} variant="light">
                    {t(`cases.status.${c.status}`)}
                  </Badge>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

export function CaseDetailPage() {
  const { t, formatDate, pick } = useI18n();
  const { memberId = "", caseId = "" } = useParams();
  const { members } = useHousehold();
  const state = members.find((m) => m.member.id === memberId);
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!state) return;
    state.client
      .case(caseId)
      .then(setDetail)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [state, caseId]);

  if (error) return <Alert color="red">{t("app.error", { message: error })}</Alert>;
  if (!detail) return <Text c="dimmed">{t("app.loading")}</Text>;
  const summary = detail.summary as { summary?: string; anomalies?: string[] } | null;
  return (
    <Stack>
      <Anchor component={Link} to="/cases" size="sm">
        ← {t("cases.back")}
      </Anchor>
      <Group>
        <Title order={2}>{pick(detail.template_titles) || detail.template_title}</Title>
        <Badge color={STATUS_COLOR[detail.status] ?? "gray"} variant="light">
          {t(`cases.status.${detail.status}`)}
        </Badge>
        <Badge variant="outline">{state?.member.title}</Badge>
      </Group>
      <Text size="sm" c="dimmed">
        {t("pending.case")} <Code>{detail.id}</Code> · {t("cases.created")} {formatDate(detail.created_at)}
        {detail.document?.original_name && ` · ${t("pending.document")}: ${detail.document.original_name}`}
      </Text>
      {(detail.parent || (detail.children && detail.children.length > 0)) && state && (
        <Text size="sm">
          {detail.parent && <>{t("cases.calledBy")} <Anchor component={Link} to={`/cases/${state.member.id}/${detail.parent.id}`}>{(detail.parent.template_titles && pick(detail.parent.template_titles)) || detail.parent.template_id}</Anchor></>}
          {detail.parent && detail.children && detail.children.length > 0 && " · "}
          {detail.children && detail.children.length > 0 && <>{t("cases.calls")} {detail.children.map((c, i) => <span key={c.id}>{i > 0 && ", "}<Anchor component={Link} to={`/cases/${state.member.id}/${c.id}`}>{(c.template_titles && pick(c.template_titles)) || c.template_id}</Anchor> ({t(`cases.status.${c.status}`)})</span>)}</>}
        </Text>
      )}
      {detail.document && state && <DocumentView client={state.client} caseId={detail.id} hasDocument={detail.document.has_file === true} name={typeof detail.document.original_name === "string" ? detail.document.original_name : null} />}
      {state && <ResultsView client={state.client} caseId={detail.id} results={detail.results} media={detail.media} stubSteps={detail.stub_steps} />}
      {state && (
        <details>
          <summary style={{ cursor: "pointer" }}><Text span size="sm">{t("cases.followUp")}</Text></summary>
          <FollowUpForm memberId={state.member.id} aboutCase={detail.id} compact />
        </details>
      )}
      {summary?.summary && (
        <div>
          <Title order={4}>{t("cases.summary")}</Title>
          <Text>{summary.summary}</Text>
          {summary.anomalies && summary.anomalies.length > 0 && (
            <>
              <Title order={5} mt="sm">{t("cases.anomalies")}</Title>
              <ul>{summary.anomalies.map((a, i) => <li key={i}>{a}</li>)}</ul>
            </>
          )}
        </div>
      )}
      <Title order={4}>{t("cases.steps")}</Title>
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{t("pending.step")}</Table.Th>
            <Table.Th>{t("settings.log.status")}</Table.Th>
            <Table.Th>{t("cases.event")}</Table.Th>
            <Table.Th>{t("cases.model")}</Table.Th>
            <Table.Th>{t("cases.when")}</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {detail.steps.map((s) => (
            <Table.Tr key={`${s.step_id}:${s.started_at}`}>
              <Table.Td><Code>{s.step_id}</Code> <Text span size="xs" c="dimmed">{s.kind}</Text></Table.Td>
              <Table.Td>{s.status}</Table.Td>
              <Table.Td>{s.outcome ?? "–"}</Table.Td>
              <Table.Td>{s.model_ref ?? "–"}</Table.Td>
              <Table.Td>{formatDate(s.finished_at ?? s.started_at)}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Title order={4}>{t("cases.audit")}</Title>
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>#</Table.Th>
            <Table.Th>{t("cases.when")}</Table.Th>
            <Table.Th>{t("cases.actor")}</Table.Th>
            <Table.Th>{t("cases.event")}</Table.Th>
            <Table.Th>{t("cases.details")}</Table.Th>
            <Table.Th>{t("cases.appVersion")}</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {detail.audit.map((a) => (
            <Table.Tr key={a.seq}>
              <Table.Td>{a.seq}</Table.Td>
              <Table.Td>{formatDate(a.at)}</Table.Td>
              <Table.Td>{a.actor_type} · {a.actor_id}</Table.Td>
              <Table.Td>{a.event}</Table.Td>
              <Table.Td><Code block>{a.details ? JSON.stringify(a.details) : ""}</Code></Table.Td>
              <Table.Td>{a.app_version}{a.model_ref ? ` · ${a.model_ref}` : ""}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
