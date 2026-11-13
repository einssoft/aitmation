// Login (ADR-0025): the launcher is the household's identity; the shell
// only collects name and password and hands them to it. Shown instead of
// the shell whenever the household requires a session and has none.
import { Alert, Button, Card, Center, PasswordInput, Select, Stack, Text, TextInput, Title } from "@mantine/core";
import { useState, type FormEvent } from "react";
import { useHousehold } from "../household";
import { LANGUAGES, LANGUAGE_NAMES, useI18n, type Language } from "../i18n";

export function LoginPage() {
  const { t, lang, setLang } = useI18n();
  const { login, launcher } = useHousehold();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(name, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Center mih="100vh" p="md">
      <Card withBorder w={380} p="lg">
        <form onSubmit={(e) => void submit(e)}>
          <Stack>
            <Title order={3}>{t("app.name")}</Title>
            <Text c="dimmed" size="sm">{t("auth.intro")}</Text>
            {launcher === null && <Alert color="yellow">{t("auth.noLauncher")}</Alert>}
            <TextInput
              label={t("auth.name")}
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
              autoComplete="username"
              autoFocus
              required
            />
            <PasswordInput
              label={t("auth.password")}
              value={password}
              onChange={(e) => setPassword(e.currentTarget.value)}
              autoComplete="current-password"
              required
            />
            {error && <Alert color="red">{t("auth.failed")}</Alert>}
            <Button type="submit" loading={busy} disabled={launcher === null}>
              {t("auth.signIn")}
            </Button>
            <Select
              size="xs"
              value={lang}
              onChange={(v) => v && setLang(v as Language)}
              data={LANGUAGES.map((l) => ({ value: l, label: LANGUAGE_NAMES[l] }))}
              aria-label={t("app.language")}
            />
          </Stack>
        </form>
      </Card>
    </Center>
  );
}
