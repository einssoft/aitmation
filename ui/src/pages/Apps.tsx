// Apps: installed solutions per instance, switch on/off, and the settings
// wizard rendered from the manifest's [settings] (ADR-0023 §2 / ADR-0021 §7).
import { Alert, Anchor, Badge, Button, Card, FileButton, Group, Modal, NumberInput, Select, Stack, Switch, Text, TextInput, Title } from "@mantine/core";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, type AppRow, type AppSetting, type CatalogEntry, type CatalogView } from "../api/client";
import { useAcrossHousehold, useHousehold } from "../household";
import { useI18n } from "../i18n";

export function AppsPage() {
  const { t, pick } = useI18n();
  const { members } = useHousehold();
  const { rows, loading, error, refresh } = useAcrossHousehold<AppRow>((c) => c.apps());
  const toggle = async (row: AppRow & { _member: { id: string } }) => {
    const client = members.find((m) => m.member.id === row._member.id)?.client;
    if (!client) return;
    await client.enableApp(row.id, !row.enabled);
    refresh();
  };
  return (
    <Stack>
      <Title order={2}>{t("apps.installed")}</Title>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      {loading && !rows.length && <Text c="dimmed">{t("app.loading")}</Text>}
      {!loading && !rows.length && <Text c="dimmed">{t("apps.empty")}</Text>}
      {rows.map((a) => (
        <Card key={`${a._member.id}:${a.id}`} withBorder>
          <Group justify="space-between" align="flex-start">
            <div>
              <Group gap="xs">
                <Anchor component={Link} to={`/apps/${a._member.id}/${a.id}`} fw={600}>
                  {a.name ? pick(a.name) : a.id}
                </Anchor>
                <Badge variant="light">{a._member.title}</Badge>
                <Badge variant="outline" color="gray">{a.version}</Badge>
                {a.trust && <TrustBadge level={a.trust.level} verified={a.trust.verified} />}
                <Badge color={a.enabled ? "green" : "gray"} variant="light">{t(a.enabled ? "apps.enabled" : "apps.disabled")}</Badge>
              </Group>
              <Text size="sm" c="dimmed">
                {a.loaded
                  ? t("apps.contains", { templates: a.templates?.length ?? 0, doctypes: a.doctypes?.length ?? 0 })
                  : t("apps.notLoaded")}
              </Text>
              {a.description && <Text size="sm" mt={4}>{pick(a.description)}</Text>}
            </div>
            <Switch checked={a.enabled} onChange={() => void toggle(a)} label={t(a.enabled ? "apps.disable" : "apps.enable")} />
          </Group>
        </Card>
      ))}
    </Stack>
  );
}

function SettingField({ setting, value, onChange }: { setting: AppSetting; value: unknown; onChange: (v: unknown) => void }) {
  const { pick, t } = useI18n();
  const label = pick(setting.label) + (setting.required ? " *" : "");
  const description = setting.help ? pick(setting.help) : undefined;
  switch (setting.type) {
    case "bool":
      return <Switch label={label} description={description} checked={Boolean(value)} onChange={(e) => onChange(e.currentTarget.checked)} />;
    case "amount":
    case "duration":
      return (
        <NumberInput
          label={label}
          description={description}
          value={typeof value === "number" ? value : value === undefined || value === null || value === "" ? "" : Number(value)}
          onChange={(v) => onChange(v === "" ? null : v)}
          suffix={setting.type === "amount" ? " CHF" : undefined}
          min={0}
        />
      );
    case "choice":
      return (
        <Select
          label={label}
          description={description}
          data={setting.choices}
          value={typeof value === "string" ? value : null}
          onChange={(v) => onChange(v)}
        />
      );
    case "principal":
    case "text":
    default:
      return (
        <TextInput
          label={label}
          description={description ?? (setting.required ? t("apps.required") : undefined)}
          value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
          onChange={(e) => onChange(e.currentTarget.value)}
        />
      );
  }
}

export function AppDetailPage() {
  const { t, pick } = useI18n();
  const { memberId = "", appId = "" } = useParams();
  const { members } = useHousehold();
  const state = members.find((m) => m.member.id === memberId);
  const [app, setApp] = useState<AppRow | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!state) return;
    state.client.app(appId).then((a) => {
      setApp(a);
      const initial: Record<string, unknown> = {};
      for (const s of a.settings ?? []) initial[s.name] = a.settings_values[s.name] ?? s.default ?? null;
      setValues(initial);
    });
  }, [state, appId]);

  if (!app || !state) return <Text c="dimmed">{t("app.loading")}</Text>;
  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await state.client.saveAppSettings(app.id, values);
      setMessage({ kind: "ok", text: t("apps.saved") });
    } catch (err) {
      setMessage({ kind: "err", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack>
      <Anchor component={Link} to="/apps" size="sm">← {t("apps.installed")}</Anchor>
      <Group>
        <Title order={2}>{app.name ? pick(app.name) : app.id}</Title>
        <Badge variant="outline" color="gray">{app.version}</Badge>
        <Badge variant="light">{state.member.title}</Badge>
      </Group>
      {app.description && <Text>{pick(app.description)}</Text>}
      {app.requires && (
        <Text size="sm" c="dimmed">
          {t("apps.requires")}: {t("apps.dataClass")} {app.requires.data_class} · {t("apps.core")} {app.requires.core}
          {app.author?.name && ` · ${t("apps.author")} ${app.author.name}`}
          {app.license && ` · ${t("apps.license")} ${app.license}`}
        </Text>
      )}
      {app.settings && app.settings.length > 0 && (
        <Card withBorder>
          <Title order={4}>{t("apps.settings")}</Title>
          <Text size="sm" c="dimmed" mb="md">{t("apps.settingsIntro")}</Text>
          <Stack>
            {app.settings.map((s) => (
              <SettingField key={s.name} setting={s} value={values[s.name]} onChange={(v) => setValues({ ...values, [s.name]: v })} />
            ))}
            {message && <Alert color={message.kind === "ok" ? "green" : "red"}>{message.text}</Alert>}
            <Group>
              <Button loading={busy} onClick={() => void save()} disabled={!app.enabled}>{t("app.save")}</Button>
            </Group>
          </Stack>
        </Card>
      )}
      <Group align="flex-start" grow>
        <Card withBorder>
          <Title order={5}>{t("apps.templates")}</Title>
          <ul>{app.templates?.map((tpl) => <li key={tpl.id}>{pick(tpl.title)} <Text span size="xs" c="dimmed">({tpl.id})</Text></li>)}</ul>
        </Card>
        <Card withBorder>
          <Title order={5}>{t("apps.doctypes")}</Title>
          <ul>{app.doctypes?.map((d) => <li key={d.id}>{pick(d.title)} <Text span size="xs" c="dimmed">→ {d.chain}</Text></li>)}</ul>
        </Card>
        <Card withBorder>
          <Title order={5}>{t("apps.tests")}</Title>
          <ul>{app.tests?.map((id) => <li key={id}>{id}</li>)}</ul>
        </Card>
      </Group>
    </Stack>
  );
}

const TRUST_COLOR: Record<string, string> = { official: "teal", verified: "blue", community: "orange", local: "gray" };

export function TrustBadge({ level, verified }: { level: string; verified?: boolean }) {
  const { t } = useI18n();
  return (
    <Badge size="sm" variant={verified === false ? "outline" : "light"} color={TRUST_COLOR[level] ?? "gray"} title={verified === false ? t("catalog.unverified") : undefined}>
      {t(`catalog.level.${level}`)}
    </Badge>
  );
}

/** Apps → Katalog: the signed catalog of the shop (ADR-0043 §4, shop v0).
 *  Entries with trust level, publisher and what they need; install or
 *  update after the digest, validator and trust check in the core; a
 *  bundle file from the community can be installed the same way. */
export function CatalogPage() {
  const { t, pick, formatDate, formatNumber } = useI18n();
  const { members } = useHousehold();
  const [memberId, setMemberId] = useState<string | null>(null);
  const [view, setView] = useState<CatalogView | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<CatalogEntry | null>(null);
  const member = members.find((m) => m.member.id === memberId) ?? members[0];
  const client = member?.client;

  const load = async (refresh = false) => {
    if (!client) return;
    setLoading(true);
    setError(null);
    try {
      setView(await client.catalog(refresh));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, [client]); // eslint-disable-line react-hooks/exhaustive-deps

  const install = async (entry: CatalogEntry) => {
    if (!client) return;
    setBusy(entry.id);
    setError(null);
    setConfirm(null);
    try {
      const result = await client.installFromCatalog(entry.id, entry.version);
      setNotice(t(result.replaced_version ? "catalog.updated" : "catalog.installed", { id: result.id, version: result.version, from: result.replaced_version ?? "" }));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const installFile = async (file: File | null) => {
    if (!file || !client) return;
    setBusy("file");
    setError(null);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      bytes.forEach((b) => { binary += String.fromCharCode(b); });
      const result = await client.installBundleFile(file.name, btoa(binary));
      setNotice(t(result.replaced_version ? "catalog.updated" : "catalog.installed", { id: result.id, version: result.version, from: result.replaced_version ?? "" }));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Stack>
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>{t("nav.catalog")}</Title>
          <Text c="dimmed" size="sm">{t("catalog.intro")}</Text>
        </div>
        <Group gap="xs">
          {members.length > 1 && <Select size="xs" data={members.map((m) => ({ value: m.member.id, label: m.member.title }))} value={member?.member.id ?? null} onChange={setMemberId} />}
          <FileButton onChange={(f) => void installFile(f)} accept=".tgz,application/gzip">
            {(props) => <Button size="xs" variant="default" loading={busy === "file"} {...props}>{t("catalog.installFile")}</Button>}
          </FileButton>
          <Button size="xs" variant="default" loading={loading} onClick={() => void load(true)}>{t("catalog.refresh")}</Button>
        </Group>
      </Group>
      {error && <Alert color="red" withCloseButton onClose={() => setError(null)}>{t("app.error", { message: error })}</Alert>}
      {notice && <Alert color="teal" withCloseButton onClose={() => setNotice(null)}>{notice}</Alert>}
      {view?.error && <Alert color="orange">{t("catalog.unavailable", { url: view.url, message: view.error })}</Alert>}
      {view && view.revoked_installed.length > 0 && (
        <Alert color="red" title={t("catalog.revokedTitle")}>
          {view.revoked_installed.map((r) => <div key={`${r.id}@${r.version}`}>{r.id} {r.installed_version}{r.reason ? ` – ${r.reason}` : ""}</div>)}
          <Text size="xs" mt={4}>{t("catalog.revokedHint")}</Text>
        </Alert>
      )}
      {view && !view.error && (
        <Text size="xs" c="dimmed">
          {t("catalog.source", { url: view.url, signed_by: view.signed_by ?? "–" })}
          {view.fetched_at ? ` · ${t("catalog.fetched")} ${formatDate(view.fetched_at)}` : ""}
          {" · "}{t("catalog.trusted", { levels: view.trusted_levels.map((l) => t(`catalog.level.${l}`)).join(", ") })}
        </Text>
      )}
      {view && !view.error && view.entries.length === 0 && <Text c="dimmed">{t("catalog.empty")}</Text>}
      {view?.entries.map((e) => (
        <Card key={`${e.id}@${e.version}`} withBorder>
          <Group justify="space-between" align="flex-start" wrap="nowrap">
            <div style={{ minWidth: 0 }}>
              <Group gap="xs" wrap="wrap">
                <Text fw={600}>{pick(e.name) || e.id}</Text>
                <Badge variant="outline" color="gray">{e.version}</Badge>
                <TrustBadge level={e.trust_level} />
                {e.state === "installed" && <Badge color="green" variant="light">{t("catalog.state.installed")}</Badge>}
                {e.state === "update" && <Badge color="yellow" variant="light">{t("catalog.state.update", { from: e.installed_version ?? "" })}</Badge>}
                {e.revoked && <Badge color="red" variant="filled">{t("catalog.state.revoked")}</Badge>}
              </Group>
              <Text size="sm" c="dimmed" mt={4}>{pick(e.description)}</Text>
              <Text size="xs" c="dimmed" mt={4}>
                {e.publisher.name ? `${t("catalog.publisher")}: ${e.publisher.name}` : ""}
                {e.publisher.name && e.model_roles.length ? " · " : ""}
                {e.model_roles.length ? `${t("catalog.modelRoles")}: ${e.model_roles.join(", ")}` : ""}
                {typeof e.requires.data_class === "string" ? ` · ${t("catalog.dataClass")}: ${e.requires.data_class}` : ""}
                {Object.keys(e.dependencies).length ? ` · ${t("catalog.dependsOn")}: ${Object.keys(e.dependencies).join(", ")}` : ""}
                {` · ${formatNumber(e.size / 1024, 0)} KB`}
              </Text>
              {e.revoked?.reason && <Text size="xs" c="red" mt={4}>{e.revoked.reason}</Text>}
            </div>
            <div>
              {e.state !== "installed" && !e.revoked && (
                <Button size="xs" disabled={!e.installable} loading={busy === e.id} onClick={() => setConfirm(e)} title={e.installable ? undefined : t("catalog.notTrusted")}>
                  {t(e.state === "update" ? "catalog.update" : "catalog.install")}
                </Button>
              )}
            </div>
          </Group>
        </Card>
      ))}
      <Modal opened={confirm !== null} onClose={() => setConfirm(null)} title={confirm ? t(confirm.state === "update" ? "catalog.confirmUpdate" : "catalog.confirmInstall", { name: pick(confirm.name) || confirm.id, version: confirm.version }) : ""}>
        {confirm && (
          <Stack gap="sm">
            <Text size="sm">{t("catalog.confirmText")}</Text>
            {confirm.model_roles.length > 0 && <Text size="sm">{t("catalog.confirmRoles", { roles: confirm.model_roles.join(", ") })}</Text>}
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setConfirm(null)}>{t("app.cancel")}</Button>
              <Button onClick={() => void install(confirm)}>{t(confirm.state === "update" ? "catalog.update" : "catalog.install")}</Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </Stack>
  );
}
