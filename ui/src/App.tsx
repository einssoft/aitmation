import { MantineProvider } from "@mantine/core";
import "@mantine/core/styles.css";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { HouseholdProvider } from "./household";
import { I18nProvider } from "./i18n";
import { AppDetailPage, AppsPage, CatalogPage } from "./pages/Apps";
import { CaseDetailPage, CasesPage } from "./pages/Cases";
import { ChatPage } from "./pages/Chat";
import { PendingPage } from "./pages/Pending";
import { LogPage, Placeholder, RulesPage, SystemPage } from "./pages/Settings";
import { ModelsPage } from "./pages/Models";
import { SetupPage } from "./pages/Setup";
import { UsersPage } from "./pages/Users";
import { DoctypesPage } from "./pages/Doctypes";
import { RecordsPage } from "./pages/Records";
import { ChannelsPage } from "./pages/Channels";
import { McpPage } from "./pages/Mcp";
import { DeadlinesPage } from "./pages/Deadlines";
import { TasksPage } from "./pages/Tasks";
import { RecordPage } from "./pages/Record";
import { TemplatesPage } from "./pages/Workshop";
import { SolutionsPage } from "./pages/Solutions";
import { AgentsPage, ProcessesPage, ToolsPage } from "./pages/Library";
import { ProjectsPage } from "./pages/Projects";
import { MetricsPage } from "./pages/Metrics";
import { OperationsPage } from "./pages/Operations";
import { KnowledgePage } from "./pages/Knowledge";
import { GroupOverview } from "./pages/Overview";
import { GROUPS } from "./nav";

const groupOf = (base: string) => GROUPS.find((g) => g.base === base)!;

export default function App() {
  return (
    <MantineProvider defaultColorScheme="auto" theme={{ primaryColor: "teal" }}>
      <I18nProvider>
        <HouseholdProvider>
          <BrowserRouter basename="/app">
            <Shell>
              <Routes>
                <Route path="/" element={<PendingPage />} />
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/deadlines" element={<DeadlinesPage />} />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/record" element={<RecordPage />} />
                <Route path="/cases" element={<CasesPage />} />
                <Route path="/cases/:memberId/:caseId" element={<CaseDetailPage />} />
                <Route path="/apps" element={<AppsPage />} />
                <Route path="/apps/catalog" element={<CatalogPage />} />
                <Route path="/apps/:memberId/:appId" element={<AppDetailPage />} />
                <Route path="/knowledge" element={<KnowledgePage />} />
                <Route path="/knowledge/records" element={<RecordsPage />} />
                <Route path="/knowledge/rules" element={<RulesPage />} />
                <Route path="/apps/improvements" element={<Placeholder titleKey="nav.improvements" textKey="placeholder.improvements" />} />
                <Route path="/workshop" element={<GroupOverview group={groupOf("/workshop")} />} />
                <Route path="/workshop/solutions" element={<SolutionsPage />} />
                <Route path="/workshop/templates" element={<TemplatesPage />} />
                <Route path="/workshop/projects" element={<ProjectsPage />} />
                <Route path="/workshop/processes" element={<ProcessesPage />} />
                <Route path="/workshop/agents" element={<AgentsPage />} />
                <Route path="/workshop/tools" element={<ToolsPage />} />
                <Route path="/workshop/*" element={<Placeholder titleKey="nav.workshop" textKey="placeholder.workshop" />} />
                <Route path="/settings" element={<GroupOverview group={groupOf("/settings")} />} />
                <Route path="/settings/setup" element={<SetupPage />} />
                <Route path="/settings/users" element={<UsersPage />} />
                <Route path="/settings/channels" element={<ChannelsPage />} />
                <Route path="/settings/doctypes" element={<DoctypesPage />} />
                <Route path="/settings/models" element={<ModelsPage />} />
                <Route path="/settings/mcp" element={<McpPage />} />
                <Route path="/settings/metrics" element={<MetricsPage />} />
                <Route path="/settings/operations" element={<OperationsPage />} />
                <Route path="/settings/log" element={<LogPage />} />
                <Route path="/settings/system" element={<SystemPage />} />
                {/* Old addresses (before ADR-0029) keep working. */}
                <Route path="/settings/records" element={<Navigate to="/knowledge/records" replace />} />
                <Route path="/settings/rules" element={<Navigate to="/knowledge/rules" replace />} />
                <Route path="/improvements" element={<Navigate to="/apps/improvements" replace />} />
              </Routes>
            </Shell>
          </BrowserRouter>
        </HouseholdProvider>
      </I18nProvider>
    </MantineProvider>
  );
}
