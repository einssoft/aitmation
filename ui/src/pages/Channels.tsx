// Einstellungen → Kanäle: every way a document reaches this instance
// (docs/VERMITTLER.md) - folder, mailbox, web form, upload - and, on the
// mailroom, the addressees it delivers to. Replaces the last HTML admin
// pages: secrets travel in bodies and are never shown again.
import { Alert, Anchor, Badge, Button, Card, Code, Group, NumberInput, PasswordInput, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { type ChannelsInfo, type NotificationRow } from "../api/client";
import { useHousehold, type MemberState } from "../household";
import { useI18n } from "../i18n";

/** Channels belong to the mailroom first (the shared inbox), so with
 * "all mandants" selected that is the instance shown. */
function useChannelMember(): MemberState | undefined {
  const { members, selected } = useHousehold();
  const reachable = members.filter((m) => m.info);
  if (selected !== "all") return reachable.find((m) => m.member.id === selected);
  return reachable.find((m) => m.member.type === "mailroom") ?? reachable[0];
}

export function ChannelsPage() {
  const { t, formatDate } = useI18n();
  const member = useChannelMember();
  const [info, setInfo] = useState<ChannelsInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [mail, setMail] = useState({ imap_host: "", imap_user: "", imap_folder: "INBOX", password: "" });
  const [mailTest, setMailTest] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ token: string; endpoint: string; header: string } | null>(null);
  const [draft, setDraft] = useState({ id: "", title: "", path: "", catch_all: false });
  const [notify, setNotify] = useState({ notify_to: "", escalate_to: "", remind_after_hours: 24, escalate_after_hours: 72, smtp_host: "", smtp_port: 587, smtp_user: "", smtp_from: "", smtp_security: "starttls" as "starttls" | "ssl" | "none", public_url: "", password: "" });
  const [notifyTestTo, setNotifyTestTo] = useState("");
  const [notifyTest, setNotifyTest] = useState<string | null>(null);
  const [recent, setRecent] = useState<NotificationRow[]>([]);

  const load = useCallback(() => {
    if (!member) return;
    member.client
      .channels()
      .then((c) => {
        setInfo(c);
        setMail({ imap_host: c.mail.imap_host, imap_user: c.mail.imap_user, imap_folder: c.mail.imap_folder, password: "" });
        const n = c.notifications;
        setNotify({ notify_to: n.notify_to, escalate_to: n.escalate_to, remind_after_hours: n.remind_after_hours, escalate_after_hours: n.escalate_after_hours, smtp_host: n.smtp_host, smtp_port: n.smtp_port, smtp_user: n.smtp_user, smtp_from: n.smtp_from, smtp_security: n.smtp_security, public_url: n.public_url, password: "" });
        setError(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
    member.client.notifications(20).then(setRecent).catch(() => setRecent([]));
  }, [member]);
  useEffect(load, [load]);

  const run = async (key: string, fn: () => Promise<void>, done?: string) => {
    setBusy(key);
    setError(null);
    setMessage(null);
    try {
      await fn();
      if (done) setMessage(done);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  if (!member) return <Text c="dimmed">{t("records.noMember")}</Text>;
  if (!info) return error ? <Alert color="red">{t("app.error", { message: error })}</Alert> : <Text c="dimmed">{t("app.loading")}</Text>;

  const status = (s: "env" | "file" | "none") => (
    <Badge variant="light" color={s === "none" ? "gray" : "green"}>{t(`settings.models.keyFrom_${s}`)}</Badge>
  );

  return (
    <Stack>
      <Group gap="xs">
        <Title order={2}>{t("channels.title")}</Title>
        <Badge variant="outline">{member.member.title}</Badge>
      </Group>
      <Text c="dimmed" size="sm">{t("channels.intro")}</Text>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {message && <Alert color="green">{message}</Alert>}

      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <Card withBorder>
          <Title order={5}>{t("channels.folder")}</Title>
          <Text size="sm" c="dimmed" mt="xs">{t("channels.folderHelp")}</Text>
          <Code block mt="xs">{info.folder.inbox_dir}</Code>
        </Card>
        <Card withBorder>
          <Title order={5}>{t("channels.upload")}</Title>
          <Text size="sm" c="dimmed" mt="xs">{t("channels.uploadHelp")}</Text>
        </Card>
      </SimpleGrid>

      <Card withBorder>
        <Group justify="space-between">
          <Title order={5}>{t("channels.mail")}</Title>
          <Badge variant="light" color={info.mail.active ? "green" : "gray"}>{info.mail.active ? t("channels.active") : t("channels.inactive")}</Badge>
        </Group>
        <Text size="sm" c="dimmed" mt="xs">{t("channels.mailHelp")}</Text>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm" mt="sm">
          <TextInput label={t("channels.imapHost")} placeholder="imap.example.ch" value={mail.imap_host} onChange={(e) => setMail({ ...mail, imap_host: e.currentTarget.value })} />
          <TextInput label={t("channels.imapUser")} placeholder="post@firma.ch" value={mail.imap_user} onChange={(e) => setMail({ ...mail, imap_user: e.currentTarget.value })} />
          <TextInput label={t("channels.imapFolder")} value={mail.imap_folder} onChange={(e) => setMail({ ...mail, imap_folder: e.currentTarget.value })} />
          <PasswordInput
            label={t("channels.imapPassword")}
            description={info.mail.password === "env" ? t("settings.models.keyFrom_env") : info.mail.password === "file" ? t("channels.passwordKept") : undefined}
            placeholder={info.mail.password === "none" ? t("settings.models.keyUnset") : "••••••••"}
            value={mail.password}
            onChange={(e) => setMail({ ...mail, password: e.currentTarget.value })}
            disabled={info.mail.password === "env"}
            autoComplete="off"
            rightSection={status(info.mail.password)}
            rightSectionWidth={110}
          />
        </SimpleGrid>
        <Group mt="sm">
          <Button loading={busy === "mail"} onClick={() => void run("mail", async () => { await member.client.saveMail({ ...mail, password: mail.password || null }); setMail((m) => ({ ...m, password: "" })); }, t("channels.mailSaved"))}>
            {t("app.save")}
          </Button>
          <Button variant="default" loading={busy === "mailtest"} onClick={() => void run("mailtest", async () => {
            const r = await member.client.testMail();
            setMailTest(r.ok ? t("channels.testOk") : r.reason === "incomplete" ? t("channels.testIncomplete") : t("channels.testFailed", { detail: r.detail ?? r.reason ?? "" }));
          })}>
            {t("channels.test")}
          </Button>
          {mailTest && <Text size="sm">{mailTest}</Text>}
        </Group>
      </Card>

      <Card withBorder>
        <Group justify="space-between">
          <Title order={5}>{t("channels.web")}</Title>
          {status(info.web.token)}
        </Group>
        <Text size="sm" c="dimmed" mt="xs">{t("channels.webHelp", { endpoint: info.web.endpoint })}</Text>
        {issued && (
          <Alert color="yellow" mt="sm" title={t("channels.tokenOnce")}>
            <Code block>{issued.token}</Code>
            <Text size="xs" mt={4}>{t("channels.tokenUse", { header: issued.header, endpoint: issued.endpoint })}</Text>
          </Alert>
        )}
        <Group mt="sm">
          <Button loading={busy === "token"} disabled={info.web.token === "env"} onClick={() => void run("token", async () => { setIssued(await member.client.issueWebToken()); })}>
            {info.web.token === "none" ? t("channels.tokenIssue") : t("channels.tokenRenew")}
          </Button>
          {info.web.token === "file" && (
            <Button variant="subtle" color="red" onClick={() => void run("revoke", async () => { await member.client.revokeWebToken(); setIssued(null); }, t("channels.tokenRevoked"))}>
              {t("channels.tokenRevoke")}
            </Button>
          )}
        </Group>
      </Card>

      <Card withBorder>
        <Group justify="space-between">
          <Title order={5}>{t("channels.notify")}</Title>
          <Badge variant="light" color={info.notifications.active ? "green" : "gray"}>{info.notifications.active ? t("channels.active") : t("channels.inactive")}</Badge>
        </Group>
        <Text size="sm" c="dimmed" mt="xs">{t("channels.notifyHelp")}</Text>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm" mt="sm">
          <TextInput label={t("channels.notifyTo")} description={t("channels.notifyToHelp")} placeholder="anna@firma.ch, buchhaltung@firma.ch" value={notify.notify_to} onChange={(e) => setNotify({ ...notify, notify_to: e.currentTarget.value })} />
          <TextInput label={t("channels.escalateTo")} description={t("channels.escalateToHelp")} value={notify.escalate_to} onChange={(e) => setNotify({ ...notify, escalate_to: e.currentTarget.value })} />
          <NumberInput label={t("channels.remindAfter")} suffix=" h" min={1} value={notify.remind_after_hours} onChange={(v) => setNotify({ ...notify, remind_after_hours: Number(v) || 24 })} />
          <NumberInput label={t("channels.escalateAfter")} suffix=" h" min={1} value={notify.escalate_after_hours} onChange={(v) => setNotify({ ...notify, escalate_after_hours: Number(v) || 72 })} />
          <TextInput label={t("channels.smtpHost")} placeholder="smtp.example.ch" value={notify.smtp_host} onChange={(e) => setNotify({ ...notify, smtp_host: e.currentTarget.value })} />
          <Group grow>
            <NumberInput label={t("channels.smtpPort")} min={1} value={notify.smtp_port} onChange={(v) => setNotify({ ...notify, smtp_port: Number(v) || 587 })} />
            <Select label={t("channels.smtpSecurity")} data={[{ value: "starttls", label: "STARTTLS" }, { value: "ssl", label: "SSL/TLS" }, { value: "none", label: t("channels.smtpNone") }]} value={notify.smtp_security} onChange={(v) => v && setNotify({ ...notify, smtp_security: v as "starttls" | "ssl" | "none" })} />
          </Group>
          <TextInput label={t("channels.smtpFrom")} placeholder="aitmation@firma.ch" value={notify.smtp_from} onChange={(e) => setNotify({ ...notify, smtp_from: e.currentTarget.value })} />
          <TextInput label={t("channels.smtpUser")} value={notify.smtp_user} onChange={(e) => setNotify({ ...notify, smtp_user: e.currentTarget.value })} />
          <PasswordInput
            label={t("channels.smtpPassword")}
            description={info.notifications.password === "env" ? t("settings.models.keyFrom_env") : info.notifications.password === "file" ? t("channels.passwordKept") : undefined}
            placeholder={info.notifications.password === "none" ? t("settings.models.keyUnset") : "••••••••"}
            value={notify.password}
            onChange={(e) => setNotify({ ...notify, password: e.currentTarget.value })}
            disabled={info.notifications.password === "env"}
            autoComplete="off"
            rightSection={status(info.notifications.password)}
            rightSectionWidth={110}
          />
          <TextInput label={t("channels.publicUrl")} description={t("channels.publicUrlHelp")} placeholder="https://haushalt.example.ch" value={notify.public_url} onChange={(e) => setNotify({ ...notify, public_url: e.currentTarget.value })} />
        </SimpleGrid>
        <Group mt="sm" align="flex-end">
          <Button loading={busy === "notify"} onClick={() => void run("notify", async () => { const { password, ...rest } = notify; await member.client.saveNotifications({ ...rest, password: password || null }); setNotify((n) => ({ ...n, password: "" })); }, t("channels.notifySaved"))}>
            {t("app.save")}
          </Button>
          <TextInput size="xs" placeholder={t("channels.testTo")} value={notifyTestTo} onChange={(e) => setNotifyTestTo(e.currentTarget.value)} />
          <Button size="xs" variant="default" loading={busy === "notifytest"} disabled={!notifyTestTo.trim()} onClick={() => void run("notifytest", async () => {
            const r = await member.client.testNotifications(notifyTestTo.trim());
            setNotifyTest(r.ok ? t("channels.testOk") : r.reason === "incomplete" ? t("channels.notifyIncomplete") : t("channels.testFailed", { detail: r.detail ?? r.reason ?? "" }));
          })}>
            {t("channels.sendTest")}
          </Button>
          {notifyTest && <Text size="sm">{notifyTest}</Text>}
        </Group>
        {recent.length > 0 && (
          <Table mt="sm" striped>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("cases.when")}</Table.Th>
                <Table.Th>{t("channels.kind")}</Table.Th>
                <Table.Th>{t("channels.recipient")}</Table.Th>
                <Table.Th>{t("channels.subject")}</Table.Th>
                <Table.Th>{t("settings.log.status")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {recent.map((r) => (
                <Table.Tr key={r.id}>
                  <Table.Td>{formatDate(r.at)}</Table.Td>
                  <Table.Td>{t(`channels.kind_${r.kind}`)}</Table.Td>
                  <Table.Td>{r.recipient}</Table.Td>
                  <Table.Td>{r.subject}</Table.Td>
                  <Table.Td>
                    <Badge size="sm" variant="light" color={r.status === "sent" ? "green" : r.status === "failed" ? "red" : "gray"}>{t(`channels.status_${r.status}`)}</Badge>
                    {r.error && <Text size="xs" c="red">{r.error}</Text>}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>

      {info.mailroom && (
        <Card withBorder>
          <Title order={5}>{t("channels.addressees")}</Title>
          <Text size="sm" c="dimmed" mt="xs">
            {t("channels.addresseesHelp")}{" "}
            <Anchor component={Link} to="/knowledge/rules" size="sm">{t("nav.rules")}</Anchor>
          </Text>
          {info.addressees.length > 0 && (
            <Table striped mt="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>ID</Table.Th>
                  <Table.Th>{t("channels.addresseeTitle")}</Table.Th>
                  <Table.Th>{t("channels.addresseePath")}</Table.Th>
                  <Table.Th>{t("channels.catchAll")}</Table.Th>
                  <Table.Th />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {info.addressees.map((a) => (
                  <Table.Tr key={a.id}>
                    <Table.Td><Code>{a.id}</Code></Table.Td>
                    <Table.Td>{a.title}</Table.Td>
                    <Table.Td><Code>{a.path}</Code></Table.Td>
                    <Table.Td>{a.catch_all && <Badge size="sm" variant="light" color="teal">{t("channels.catchAll")}</Badge>}</Table.Td>
                    <Table.Td>
                      <Group gap={4} justify="flex-end">
                        <Button size="compact-xs" variant="subtle" onClick={() => setDraft({ id: a.id, title: a.title, path: a.path, catch_all: a.catch_all })}>{t("doctypes.edit")}</Button>
                        <Button size="compact-xs" variant="subtle" color="red" disabled={a.catch_all} onClick={() => void run(`del:${a.id}`, () => member.client.deleteAddressee(a.id).then(() => undefined), t("channels.addresseeRemoved"))}>{t("doctypes.remove")}</Button>
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="sm" mt="sm">
            <TextInput label="ID" description={t("channels.idHelp")} value={draft.id} onChange={(e) => setDraft({ ...draft, id: e.currentTarget.value.toLowerCase() })} />
            <TextInput label={t("channels.addresseeTitle")} value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.currentTarget.value })} />
            <TextInput label={t("channels.addresseePath")} description={t("channels.addresseePathHelp")} value={draft.path} onChange={(e) => setDraft({ ...draft, path: e.currentTarget.value })} />
          </SimpleGrid>
          <Group mt="sm">
            <Switch label={t("channels.catchAllGrant")} description={t("channels.catchAllHelp")} checked={draft.catch_all} onChange={(e) => setDraft({ ...draft, catch_all: e.currentTarget.checked })} />
          </Group>
          <Group mt="sm">
            <Button loading={busy === "addressee"} disabled={!draft.id.trim() || !draft.title.trim() || !draft.path.trim()} onClick={() => void run("addressee", async () => { await member.client.saveAddressee(draft.id.trim(), { title: draft.title, path: draft.path, catch_all: draft.catch_all }); setDraft({ id: "", title: "", path: "", catch_all: false }); }, t("channels.addresseeSaved"))}>
              {t("app.save")}
            </Button>
            <Button variant="default" onClick={() => setDraft({ id: "", title: "", path: "", catch_all: false })}>{t("app.cancel")}</Button>
          </Group>
        </Card>
      )}
    </Stack>
  );
}
