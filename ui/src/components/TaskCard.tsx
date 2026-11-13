// A client task in the inbox (ADR-0041 §4): what a tool needs from a
// person (or a browser actuator) - the instruction, the bound inputs, and
// a file or data to deliver. One submit claims and finishes the task;
// the process continues on the next tick.
import { Alert, Badge, Button, Card, Code, CopyButton, FileInput, Group, Stack, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, type ClientTask, type Member } from "../api/client";
import { useHousehold } from "../household";
import { useI18n } from "../i18n";

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

const ACCEPT: Record<string, string> = { image: "image/*", video: "video/*", audio: "audio/*", text: "*/*" };

export function TaskCard({ task, onDone }: { task: ClientTask & { _member: Member }; onDone: () => void }) {
  const { t, pick, formatDate } = useI18n();
  const { members } = useHousehold();
  const client = members.find((m) => m.member.id === task._member.id)?.client;
  const [file, setFile] = useState<File | null>(null);
  const [data, setData] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const params = task.params;
  const expectsFile = params.expects !== "data";
  const inputs = params.inputs ?? {};
  const outputs = Object.keys(params.outputs ?? {}).filter((k) => k !== "file");
  const asText = (value: unknown) => (typeof value === "string" ? value : Array.isArray(value) ? value.map(String).join("\n") : JSON.stringify(value, null, 1));
  // Everything the generator needs in one block: prompt first, then the rest.
  const allText = Object.entries(inputs)
    .sort(([a], [b]) => (a === "prompt" ? -1 : b === "prompt" ? 1 : 0))
    .map(([k, v]) => (k === "prompt" ? asText(v) : `${k}:\n${asText(v)}`))
    .join("\n\n");
  // A pasted image (Ctrl+V anywhere on the card) counts as the file.
  const onPaste = (e: React.ClipboardEvent) => {
    const item = Array.from(e.clipboardData?.files ?? [])[0];
    if (item && expectsFile) { setFile(item); e.preventDefault(); }
  };

  const deliver = async () => {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      await client.fulfilClientTask(task.id, {
        file: file ? { name: file.name, content_base64: await toBase64(file) } : undefined,
        data: Object.fromEntries(Object.entries(data).filter(([, v]) => v !== "")),
      });
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card withBorder padding="md" style={{ borderColor: "var(--mantine-color-violet-3)" }} onPaste={onPaste}>
      <Group justify="space-between" align="flex-start">
        <div style={{ minWidth: 0, flex: 1 }}>
          <Group gap="xs">
            <Badge color="violet" variant="filled">{t("tasks.deliver")}</Badge>
            <Text fw={600}>{pick(params.name) || params.tool || task.action}</Text>
            <Badge variant="light">{task._member.title}</Badge>
            {task.app && <Badge variant="outline" color="gray">{task.app}</Badge>}
            {params.kind && <Badge variant="outline" color="violet">{t(`tasks.kind.${params.kind}`)}</Badge>}
            {task.status === "claimed" && <Badge color="yellow" variant="light">{t("tasks.claimedBy", { who: task.claimed_by ?? "?" })}</Badge>}
          </Group>
          <Text size="sm" c="dimmed">
            {pick(task.template_title) || task.template_id} · <code>{task.step_id}</code> · {t("pending.waitingSince", { when: formatDate(task.created_at) })}
          </Text>
          {params.instruction && <Text size="sm" mt={6}>{pick(params.instruction)}</Text>}
          <Group gap="xs" mt={6}>
            {Object.keys(inputs).length > 1 && (
              <CopyButton value={allText}>{({ copied, copy }) => <Button size="compact-xs" variant="light" onClick={copy}>{copied ? t("tasks.copied") : t("tasks.copyAll")}</Button>}</CopyButton>
            )}
            {Object.entries(params.links ?? {}).map(([label, href]) => (
              // Copies the prompt, then opens the generator in a new tab - paste and go.
              <Button key={label} size="compact-xs" variant="default" component="a" href={href} target="_blank" rel="noopener noreferrer"
                onClick={() => { void navigator.clipboard?.writeText(allText).catch(() => undefined); }}>
                {t("tasks.openIn", { name: label })}
              </Button>
            ))}
          </Group>
          <Stack gap={4} mt="sm">
            {Object.entries(inputs).map(([name, value]) => {
              const text = asText(value);
              // A delivered medium of this case ("media/<case>/<file>"): offer it as a download.
              const media = typeof value === "string" && value.startsWith(`media/${task.case_id}/`) && client ? client.mediaUrl(task.case_id, value.split("/").pop() ?? "") : null;
              return (
                <div key={name}>
                  <Group gap="xs" justify="space-between">
                    <Text size="xs" c="dimmed" tt="uppercase" fw={700}>{name}</Text>
                    {media ? <Button size="compact-xs" variant="subtle" component="a" href={media} target="_blank" rel="noopener">{t("tasks.download")}</Button> : <CopyButton value={text}>{({ copied, copy }) => <Button size="compact-xs" variant="subtle" onClick={copy}>{copied ? t("tasks.copied") : t("tasks.copy")}</Button>}</CopyButton>}
                  </Group>
                  {media ? <Text size="sm">{String(value).split("/").pop()}</Text> : <Code block style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{text}</Code>}
                </div>
              );
            })}
          </Stack>
        </div>
        <Button component={Link} to={`/cases/${task._member.id}/${task.case_id}`} variant="subtle" size="xs">{t("pending.case")}</Button>
      </Group>
      <Group mt="md" align="flex-end" gap="sm">
        {expectsFile && <FileInput label={t("tasks.file")} description={t("tasks.pasteHint")} placeholder={t("tasks.filePlaceholder")} accept={ACCEPT[params.kind ?? "text"]} value={file} onChange={setFile} style={{ minWidth: 280 }} />}
        {outputs.map((name) => (
          <TextInput key={name} label={name} description={params.outputs?.[name]} value={data[name] ?? ""} onChange={(e) => setData((d) => ({ ...d, [name]: e.currentTarget.value }))} />
        ))}
        <Button loading={busy} disabled={expectsFile && !file} onClick={deliver}>{t("tasks.submit")}</Button>
      </Group>
      {error && <Alert color="red" mt="sm">{t("app.error", { message: error })}</Alert>}
    </Card>
  );
}
