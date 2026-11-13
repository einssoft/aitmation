// Navigation by audience and task (ADR-0021, grouped per ADR-0029): the
// daily entries stay flat (Pendenzen · Chat · Vorgänge · Fristen); Wissen,
// Apps, [Werkstatt] and Verwaltung fold into groups with a landing page,
// so the sidebar does not grow by one line per function. The workshop is
// the admin's place and always shown to admins (and to everyone while the
// household has no login yet); members see it only with the "advanced
// view" switch (header, next to household and language). The navbar
// scrolls on small screens.
import {
  AppShell,
  Badge,
  Burger,
  Button,
  Group,
  NavLink,
  ScrollArea,
  Select,
  Switch,
  Text,
  Title,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink as RouterLink, useLocation } from "react-router-dom";
import { LANGUAGES, LANGUAGE_NAMES, useI18n, type Language } from "../i18n";
import { useHousehold } from "../household";
import { GROUPS, WORK, childActive, isActive, type NavGroup } from "../nav";
import { LoginPage } from "../pages/Login";

const ADVANCED_KEY = "aitmation.advanced";

function readAdvanced(): boolean {
  try {
    return localStorage.getItem(ADVANCED_KEY) === "1";
  } catch {
    return false;
  }
}

export function Shell({ children }: { children: ReactNode }) {
  const { t, lang, setLang } = useI18n();
  const {
    members,
    selected,
    select,
    authRequired,
    principal,
    logout,
    loading,
  } = useHousehold();
  const [opened, { toggle, close }] = useDisclosure();
  const [advanced, setAdvanced] = useState(readAdvanced);
  // Groups fold; the one holding the current route is open, others as toggled.
  const [manual, setManual] = useState<Record<string, boolean>>({});
  const location = useLocation();
  useEffect(close, [location.pathname, close]);
  useEffect(() => {
    try {
      localStorage.setItem(ADVANCED_KEY, advanced ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [advanced]);

  // The workshop belongs to the admin: no switch needed for them. Without
  // a login everyone is the admin of their own installation.
  const isAdmin = !authRequired || principal?.role === "admin";

  // ADR-0025: a household with users shows the login until a session exists.
  if (authRequired && !principal) {
    if (loading)
      return (
        <Text p="md" c="dimmed">
          {t("app.loading")}
        </Text>
      );
    return <LoginPage />;
  }

  const openGates = members.reduce((n, m) => n + (m.info?.open_gates ?? 0), 0);
  const badgeOf = (to: string) =>
    to === "/" && openGates ? (
      <Badge size="sm" circle>
        {openGates}
      </Badge>
    ) : undefined;
  const item = (to: string, label: string, active: boolean, nested = false) => (
    <NavLink
      key={to}
      component={RouterLink}
      to={to}
      label={label}
      rightSection={badgeOf(to)}
      active={active}
      pl={nested ? 28 : undefined}
    />
  );
  const group = (g: NavGroup) => {
    const inGroup = isActive(location.pathname, g.base);
    const opened = manual[g.base] ?? inGroup;
    return (
      <NavLink
        key={g.base}
        component={RouterLink}
        to={g.base}
        label={t(`nav.${g.key}`)}
        active={location.pathname === g.base}
        opened={opened}
        onChange={(o) => setManual((m) => ({ ...m, [g.base]: o }))}
        childrenOffset={0}
      >
        {g.children.map((c) => item(c.to, t(`nav.${c.key}`), childActive(location.pathname, g, c), true))}
      </NavLink>
    );
  };
  const section = (label: string, links: ReactNode) => (
    <div key={label}>
      <Text size="xs" fw={700} c="dimmed" tt="uppercase" px="sm" pt="md" pb={4}>
        {label}
      </Text>
      {links}
    </div>
  );

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 240, breakpoint: "sm", collapsed: { mobile: !opened } }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <Burger
              opened={opened}
              onClick={toggle}
              hiddenFrom="sm"
              size="sm"
            />
            <Title order={4}>{t("app.name")}</Title>
          </Group>
          <Group gap="sm">
            {members.length > 1 && (
              <Select
                size="xs"
                w={200}
                value={selected}
                onChange={(v) => select((v as string | null) ?? "all")}
                data={[
                  { value: "all", label: t("household.all") },
                  ...members.map((m) => ({
                    value: m.member.id,
                    label: m.info
                      ? m.member.title
                      : t("household.unreachable", { title: m.member.title }),
                    disabled: !m.info,
                  })),
                ]}
                aria-label={t("household.members")}
              />
            )}
            {principal && (
              <Group gap={6}>
                <Text size="sm">{principal.sub}</Text>
                <Button
                  size="compact-xs"
                  variant="subtle"
                  onClick={() => void logout()}
                >
                  {t("auth.signOut")}
                </Button>
              </Group>
            )}
            <Switch
              size="xs"
              label={t("app.advanced")}
              checked={advanced}
              onChange={(e) => setAdvanced(e.currentTarget.checked)}
              visibleFrom="sm"
            />
            <Select
              size="xs"
              w={120}
              value={lang}
              onChange={(v) => v && setLang(v as Language)}
              data={LANGUAGES.map((l) => ({
                value: l,
                label: LANGUAGE_NAMES[l],
              }))}
              aria-label={t("app.language")}
            />
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        <ScrollArea h="100%" type="auto" offsetScrollbars>
          {section(
            t("nav.work"),
            WORK.map((e) => item(e.to, t(`nav.${e.key}`), isActive(location.pathname, e.to))),
          )}
          {section(
            t("nav.more"),
            GROUPS.filter((g) => !g.advanced || advanced || isAdmin).map(group),
          )}
          <Switch
            mt="lg"
            mb="sm"
            mx="sm"
            size="xs"
            label={t("app.advanced")}
            checked={advanced}
            onChange={(e) => setAdvanced(e.currentTarget.checked)}
            hiddenFrom="sm"
          />
        </ScrollArea>
      </AppShell.Navbar>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}
