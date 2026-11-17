// The navigation model (ADR-0029, refining ADR-0021): a few flat entries
// for daily work, then groups that fold - each group has a landing page
// that lists its members. One source for the sidebar and the overviews.
import {
  IconActivity,
  IconAdjustmentsHorizontal,
  IconApps,
  IconBook2,
  IconBooks,
  IconBuildingStore,
  IconBulb,
  IconCalendarDue,
  IconChartBar,
  IconChecklist,
  IconDatabase,
  IconFileDescription,
  IconFolderCog,
  IconFolders,
  IconInbox,
  IconInfoCircle,
  IconListSearch,
  IconMailbox,
  IconMessage,
  IconMicrophone,
  IconPackage,
  IconPlug,
  IconPlugConnected,
  IconPuzzle,
  IconRobot,
  IconRoute,
  IconSettings,
  IconShieldCheck,
  IconTool,
  IconUsers,
  IconWand,
  type Icon,
} from "@tabler/icons-react";

// Icons: Tabler (MIT), one per entry, drawn as thin lines (see ICON below).
export const ICON = { size: 20, stroke: 1.5 } as const;

export interface NavEntry {
  to: string;
  key: string; // i18n key under nav.*
  icon: Icon;
}

export interface NavGroup {
  base: string; // landing route
  key: string; // i18n key under nav.*
  icon: Icon;
  advanced?: boolean; // only with the advanced view
  children: NavEntry[];
}

export const WORK: NavEntry[] = [
  { to: "/", key: "pending", icon: IconInbox },
  { to: "/chat", key: "chat", icon: IconMessage },
  { to: "/cases", key: "cases", icon: IconFolders },
  { to: "/deadlines", key: "deadlines", icon: IconCalendarDue },
  { to: "/tasks", key: "tasks", icon: IconChecklist },
  { to: "/record", key: "record", icon: IconMicrophone },
];

export const GROUPS: NavGroup[] = [
  {
    base: "/knowledge",
    key: "knowledge", icon: IconBook2,
    children: [
      { to: "/knowledge", key: "spaces", icon: IconBooks },
      { to: "/knowledge/records", key: "records", icon: IconDatabase },
      { to: "/knowledge/rules", key: "rules", icon: IconShieldCheck },
    ],
  },
  {
    base: "/apps",
    key: "apps", icon: IconApps,
    children: [
      { to: "/apps", key: "installed", icon: IconPackage },
      { to: "/apps/catalog", key: "catalog", icon: IconBuildingStore },
      { to: "/apps/improvements", key: "improvements", icon: IconBulb },
    ],
  },
  {
    base: "/workshop",
    key: "workshop", icon: IconTool,
    advanced: true,
    children: [
      { to: "/workshop/projects", key: "projects", icon: IconFolderCog },
      { to: "/workshop/processes", key: "processes", icon: IconRoute },
      { to: "/workshop/agents", key: "agents", icon: IconRobot },
      { to: "/workshop/tools", key: "tools", icon: IconPlug },
      { to: "/workshop/solutions", key: "solutions", icon: IconPuzzle },
    ],
  },
  {
    base: "/settings",
    key: "settings", icon: IconSettings,
    children: [
      { to: "/settings/system", key: "system", icon: IconInfoCircle },
      { to: "/settings/operations", key: "operations", icon: IconActivity },
      { to: "/settings/metrics", key: "metrics", icon: IconChartBar },
      { to: "/settings/users", key: "users", icon: IconUsers },
      { to: "/settings/channels", key: "channels", icon: IconMailbox },
      { to: "/settings/doctypes", key: "doctypes", icon: IconFileDescription },
      { to: "/settings/models", key: "models", icon: IconAdjustmentsHorizontal },
      { to: "/settings/mcp", key: "mcp", icon: IconPlugConnected },
      { to: "/settings/log", key: "log", icon: IconListSearch },
      { to: "/settings/setup", key: "setup", icon: IconWand },
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
