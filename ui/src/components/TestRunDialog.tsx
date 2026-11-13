// "Testlauf" in the designer (ADR-0062): fill the start form, run the
// saved process right away (POST /templates/{id}/run, ADR-0054) and see
// each step's outcome and output. Gates stop the run like in real life -
// nothing goes out that a person has not released.
import { Alert, Badge, Button, Code, Group, Modal, ScrollArea, Stack, Text, TextInput, Textarea } from "@mantine/core";
import { useEffect, useState } from "react";
import type { ContractInput, RunReport } from "../api/client";
import { useI18n } from "../i18n";

export function TestRunDialog({ opened, processId, contract, onClose, onRun }: {
  opened: boolean;
  processId: string;
  contract: ContractInput[];
  onClose: () => void;
  onRun: (payload: Record<string, unknown>) => Promise<RunReport>;
}) {
  const { t } = useI18n();
  const [values, setValues] = useState<Record<string, string>>({});
  const [report, setReport] = useState<RunReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!opened) return;
    setValues(Object.fromEntries(contract.map((c) => [c.name, c.default ?? ""])));
    setReport(null);
    setError(null);
  }, [opened, contract]);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(values)) {
        const text = v.trim();
        if (text.startsWith("[") || text.startsWith("{")) {
          try { payload[k] = JSON.parse(text); continue; } catch { /* plain text */ }
        }
        payload[k] = v;
      }
      setReport(await onRun(payload));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const color = report?.status === "completed" ? "teal" : report?.status === "failed" ? "red" : "orange";
  return (
    <Modal opened={opened} onClose={onClose} size="lg" title={t("solutions.designer.testRunTitle", { id: processId })}>
      <Stack gap="xs">
        <Text size="xs" c="dimmed">{t("solutions.designer.testRunHint")}</Text>
        {contract.map((c) => (
          c.description.length > 60
            ? <Textarea key={c.name} size="xs" autosize minRows={2} label={c.name} description={c.description} required={c.default === undefined} value={values[c.name] ?? ""} onChange={(e) => setValues({ ...values, [c.name]: e.currentTarget.value })} />
            : <TextInput key={c.name} size="xs" label={c.name} description={c.description} required={c.default === undefined} value={values[c.name] ?? ""} onChange={(e) => setValues({ ...values, [c.name]: e.currentTarget.value })} />
        ))}
        {!contract.length && <Text size="xs" c="dimmed">{t("solutions.designer.noStartForm")}</Text>}
        <Group justify="flex-end">
          <Button size="xs" variant="subtle" onClick={onClose}>{t("solutions.cancel")}</Button>
          <Button size="xs" loading={busy} onClick={() => void run()}>{t("solutions.designer.runNow")}</Button>
        </Group>
        {error && <Alert color="red">{error}</Alert>}
        {report && (
          <Stack gap={6}>
            <Group gap="xs">
              <Badge color={color}>{t(`solutions.designer.status.${report.status}`)}</Badge>
              {report.waiting_on.length > 0 && <Text size="xs">{t("solutions.designer.waitingOn", { steps: report.waiting_on.map((w) => w.step_id).join(", ") })}</Text>}
              <Text size="xs" c="dimmed">{report.case_id}</Text>
            </Group>
            <ScrollArea.Autosize mah={360} type="auto">
              <Stack gap={6}>
                {Object.entries(report.outputs).map(([step, out]) => (
                  <div key={step}>
                    <Group gap={6}><Text size="xs" fw={700}>{step}</Text><Badge size="xs" variant="light" color={out.outcome === "failed" ? "red" : "gray"}>{out.outcome}</Badge></Group>
                    <Code block style={{ fontSize: 11, maxHeight: 160, overflow: "auto" }}>{JSON.stringify(out.result, null, 2)}</Code>
                  </div>
                ))}
              </Stack>
            </ScrollArea.Autosize>
          </Stack>
        )}
      </Stack>
    </Modal>
  );
}
