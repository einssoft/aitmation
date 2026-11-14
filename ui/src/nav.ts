// The navigation model (ADR-0029, refining ADR-0021): a few flat entries
// for daily work, then groups that fold - each group has a landing page
// that lists its members. One source for the sidebar and the overviews.
export interface NavEntry {
  to: string;
  key: string; // i18n key under nav.*
}

export interface NavGroup {
  base: string; // landing route
  key: string; // i18n key under nav.*
  advanced?: boolean; // only with the advanced view
  children: NavEntry[];
}

export const WORK: NavEntry[] = [
  { to: "/", key: "pending" },
  { to: "/chat", key: "chat" },
  { to: "/cases", key: "cases" },
  { to: "/deadlines", key: "deadlines" },
  { to: "/tasks", key: "tasks" },
  { to: "/record", key: "record" },
];

export const GROUPS: NavGroup[] = [
  {
    base: "/knowledge",
    key: "knowledge",
    children: [
      { to: "/knowledge", key: "spaces" },
      { to: "/knowledge/records", key: "records" },
      { to: "/knowledge/rules", key: "rules" },
    ],
  },
  {
    base: "/apps",
    key: "apps",
    children: [
      { to: "/apps", key: "installed" },
      { to: "/apps/catalog", key: "catalog" },
      { to: "/apps/improvements", key: "improvements" },
    ],
  },
  {
    base: "/workshop",
    key: "workshop",
    advanced: true,
    children: [
      { to: "/workshop/projects", key: "projects" },
      { to: "/workshop/processes", key: "processes" },
      { to: "/workshop/agents", key: "agents" },
      { to: "/workshop/tools", key: "tools" },
      { to: "/workshop/solutions", key: "solutions" },
    ],
  },
  {
    base: "/settings",
    key: "settings",
    children: [
      { to: "/settings/system", key: "system" },
      { to: "/settings/operations", key: "operations" },
      { to: "/settings/metrics", key: "metrics" },
      { to: "/settings/users", key: "users" },
      { to: "/settings/channels", key: "channels" },
      { to: "/settings/doctypes", key: "doctypes" },
      { to: "/settings/models", key: "models" },
      { to: "/settings/mcp", key: "mcp" },
      { to: "/settings/log", key: "log" },
      { to: "/settings/setup", key: "setup" },
    ],
  },
];

export function isActive(pathname: string, to: string, exact = false): boolean {
  if (pathname === to) return true;
  if (exact || to === "/") return false;
  return pathname.startsWith(`${to}/`);
}

/** A child is exact when it is the group's own landing route. */
export function childActive(pathname: string, group: NavGroup, child: NavEntry): boolean {
  if (child.to === group.base) {
    // The landing route is active only when no sibling matches.
    return pathname === group.base || (isActive(pathname, group.base) && !group.children.some((c) => c.to !== group.base && isActive(pathname, c.to)));
  }
  return isActive(pathname, child.to);
}
