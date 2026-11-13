// Einrichtung: the household wizard (ADR-0024). Mandants are created by the
// launcher; this page asks "private, company or both?" and posts to it.
// Without a launcher (single instance in development) it explains why.
// Step two is the profile per mandant (ADR-0026): country, name,
// identifiers - filled in right here, form generated from the schema.
import { Alert, Badge, Button, Card, Group, List, Radio, Stack, Text, TextInput, Title } from "@mantine/core";
import { useEffect, useState } from "react";
import { type RecordRow } from "../api/client";
import { useHousehold, type MemberState } from "../household";
import { useI18n } from "../i18n";
import { RecordForm } from "./Records";

function ProfileStep({ member }: { member: MemberState }) {
  const { t, pick } = useI18n();
  const [rows, setRows] = useState<RecordRow[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    // The wizard step is about the profile - collections are filled in later.
    member.client.records().then((r) => setRows(r.filter((x) => x.kind !== "collection"))).catch(() => setRows([]));
  }, [member, tick]);
  if (!rows.length) return null;
  return (
    <Stack gap="xs">
      {rows.map((r) => (
        <div key={r.id}>
          <Group justify="space-between">
            <Group gap="xs">
              <Text fw={500}>{member.member.title}</Text>
              <Text size="sm" c="dimmed">{pick(r.title)}</Text>
              {r.completeness && (
                <Badge size="sm" color={r.completeness.complete ? "green" : "yellow"} variant="light">
                  {r.completeness.complete ? t("records.complete") : t("records.incomplete", { filled: r.completeness.filled, required: r.completeness.required })}
                </Badge>
              )}
            </Group>
            <Button size="compact-xs" variant={open === r.id ? "default" : "light"} onClick={() => setOpen(open === r.id ? null : r.id)}>
              {open === r.id ? t("setup.close") : t("setup.fill")}
            </Button>
          </Group>
          {open === r.id && (
            <Card withBorder mt="xs" padding="sm">
              <RecordForm member={member} recordId={r.id} compact onSaved={() => setTick((n) => n + 1)} />
            </Card>
          )}
        </div>
      ))}
    </Stack>
  );
}

export function SetupPage() {
  const { t } = useI18n();
  const { members, launcher, reload, launcherClient } = useHousehold();
  const [kind, setKind] = useState<"private" | "company" | "both">("both");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const add = async () => {
    if (launcher === null) return;
    setBusy(true);
    setMessage(null);
    try {
      const wanted = kind === "both" ? ["private", "company"] : [kind];
      for (const type of wanted) {
        const title = type === "private" ? t("household.private") : name || t("household.company");
        await launcherClient!.addMandant(type, title);
      }
      setMessage({ kind: "ok", text: t("setup.created") });
      setName("");
      await reload();
    } catch (err) {
      setMessage({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack>
      <Title order={2}>{t("setup.title")}</Title>
      <Text>{t("setup.intro")}</Text>
      <Card withBorder>
        <Title order={5}>{t("setup.members")}</Title>
        <List mt="sm">
          {members.map((m) => (
            <List.Item key={m.member.id}>
              {m.member.title} <Text span size="xs" c="dimmed">({m.member.type}{m.error ? ` · ${t("household.unreachable", { title: "" }).trim()}` : ""})</Text>
            </List.Item>
          ))}
        </List>
      </Card>
      <Card withBorder>
        <Title order={5}>{t("setup.add")}</Title>
        {launcher === null && <Alert color="yellow" mt="sm">{t("setup.launcherMissing")}</Alert>}
        <Stack mt="sm">
          <Radio.Group label={t("setup.kind")} value={kind} onChange={(v) => setKind(v as typeof kind)}>
            <Group mt="xs">
              <Radio value="private" label={t("setup.kindPrivate")} />
              <Radio value="company" label={t("setup.kindCompany")} />
              <Radio value="both" label={t("setup.kindBoth")} />
            </Group>
          </Radio.Group>
          {kind !== "private" && <TextInput label={t("setup.name")} value={name} onChange={(e) => setName(e.currentTarget.value)} placeholder="Firma X" />}
          {message && <Alert color={message.kind === "ok" ? "green" : "red"}>{message.text}</Alert>}
          <Group>
            <Button disabled={launcher === null} loading={busy} onClick={() => void add()}>{t("setup.add")}</Button>
          </Group>
        </Stack>
      </Card>
      {members.some((m) => m.info && m.member.type !== "mailroom") && (
        <Card withBorder>
          <Title order={5}>{t("setup.profiles")}</Title>
          <Text size="sm" c="dimmed" mb="sm">{t("setup.profilesIntro")}</Text>
          <Stack gap="md">
            {members.filter((m) => m.info && m.member.type !== "mailroom").map((m) => (
              <ProfileStep key={m.member.id} member={m} />
            ))}
          </Stack>
        </Card>
      )}
      <Card withBorder>
        <Title order={5}>{t("setup.steps")}</Title>
        <List type="ordered" mt="sm">
          <List.Item>{t("setup.step1")}</List.Item>
          <List.Item>{t("setup.step2b")}</List.Item>
          <List.Item>{t("setup.step2")}</List.Item>
          <List.Item>{t("setup.step3")}</List.Item>
          <List.Item>{t("setup.step4")}</List.Item>
        </List>
      </Card>
    </Stack>
  );
}
