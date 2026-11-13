// Einstellungen → Benutzer (ADR-0025): the household's people, managed
// through the launcher. Admins add and remove; everyone can change their
// own password. Roles are coarse in v1 (admin | member with instances);
// per-app and per-gate assignment (ADR-0015) is the next step.
import { Alert, Badge, Button, Card, Group, MultiSelect, PasswordInput, Select, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { type HouseholdUser } from "../api/client";
import { useHousehold } from "../household";
import { useI18n } from "../i18n";

export function UsersPage() {
  const { t, formatDate } = useI18n();
  const { members, principal, authRequired, launcherClient } = useHousehold();
  const [users, setUsers] = useState<HouseholdUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<string>("member");
  const [instances, setInstances] = useState<string[]>([]);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const isAdmin = principal?.role === "admin";

  const load = useCallback(() => {
    if (!launcherClient || !isAdmin) return;
    launcherClient
      .users()
      .then(setUsers)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [launcherClient, isAdmin]);
  useEffect(load, [load]);

  const run = async (action: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await action();
      setMessage(done);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!authRequired) {
    return (
      <Stack>
        <Title order={2}>{t("nav.users")}</Title>
        <Alert color="yellow">{t("auth.off")}</Alert>
      </Stack>
    );
  }

  const mandants = members.filter((m) => m.member.type !== "mailroom").map((m) => ({ value: m.member.id, label: m.member.title }));

  return (
    <Stack>
      <Title order={2}>{t("nav.users")}</Title>
      <Text c="dimmed" size="sm">{t("auth.usersIntro")}</Text>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {message && <Alert color="green">{message}</Alert>}

      {isAdmin && (
        <>
          <Table striped>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("auth.name")}</Table.Th>
                <Table.Th>{t("auth.role")}</Table.Th>
                <Table.Th>{t("household.members")}</Table.Th>
                <Table.Th>{t("cases.created")}</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {users.map((u) => (
                <Table.Tr key={u.name}>
                  <Table.Td>{u.name}</Table.Td>
                  <Table.Td><Badge variant="light">{t(`auth.role_${u.role}`)}</Badge></Table.Td>
                  <Table.Td>{u.instances.includes("*") ? t("household.all") : u.instances.join(", ")}</Table.Td>
                  <Table.Td>{formatDate(u.created_at)}</Table.Td>
                  <Table.Td>
                    {u.name !== principal?.sub && (
                      <Button size="xs" variant="subtle" color="red" loading={busy} onClick={() => void run(() => launcherClient!.removeUser(u.name), t("auth.removed"))}>
                        {t("auth.remove")}
                      </Button>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          <Card withBorder>
            <Title order={5}>{t("auth.add")}</Title>
            <Stack mt="sm">
              <Group grow>
                <TextInput label={t("auth.name")} value={name} onChange={(e) => setName(e.currentTarget.value)} />
                <PasswordInput label={t("auth.password")} value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
              </Group>
              <Group grow>
                <Select
                  label={t("auth.role")}
                  value={role}
                  onChange={(v) => setRole(v ?? "member")}
                  data={[
                    { value: "member", label: t("auth.role_member") },
                    { value: "admin", label: t("auth.role_admin") },
                  ]}
                />
                {role === "member" && (
                  <MultiSelect label={t("household.members")} data={mandants} value={instances} onChange={setInstances} />
                )}
              </Group>
              <Group>
                <Button
                  loading={busy}
                  disabled={!name || !password || (role === "member" && instances.length === 0)}
                  onClick={() =>
                    void run(async () => {
                      await launcherClient!.addUser({ name, password, role, instances });
                      setName("");
                      setPassword("");
                      setInstances([]);
                    }, t("auth.added"))
                  }
                >
                  {t("auth.add")}
                </Button>
              </Group>
            </Stack>
          </Card>
        </>
      )}

      <Card withBorder>
        <Title order={5}>{t("auth.changePassword")}</Title>
        <Stack mt="sm">
          <Group grow>
            <PasswordInput label={t("auth.currentPassword")} value={current} onChange={(e) => setCurrent(e.currentTarget.value)} autoComplete="current-password" />
            <PasswordInput label={t("auth.newPassword")} value={next} onChange={(e) => setNext(e.currentTarget.value)} autoComplete="new-password" />
          </Group>
          <Group>
            <Button
              loading={busy}
              disabled={!current || next.length < 8}
              onClick={() =>
                void run(async () => {
                  await launcherClient!.changePassword(current, next);
                  setCurrent("");
                  setNext("");
                }, t("auth.passwordChanged"))
              }
            >
              {t("app.save")}
            </Button>
          </Group>
        </Stack>
      </Card>
    </Stack>
  );
}
