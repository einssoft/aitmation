// Typed access to /api/v1 of one instance. The household shell keeps one
// client per member (ADR-0024); the member whose origin served this page
// is addressed relatively so the Vite proxy works in development.

/** Who is signed in (ADR-0025); null when the household has no users. */
export interface Principal {
  sub: string;
  role: "admin" | "member" | string;
  instances: string[];
  exp: number;
}

export interface HouseholdUser {
  name: string;
  role: string;
  instances: string[];
  created_at: string;
}

/** The launcher's identity endpoints; cookie-based. `url` is "" behind the launcher (ADR-0032). */
export class LauncherClient {
  constructor(public readonly url: string) {}

  private async call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${this.url}${path}`, {
      ...init,
      credentials: "include",
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
    if (!resp.ok) throw new ApiError((await resp.text()) || resp.statusText, resp.status);
    return (await resp.json()) as T;
  }

  me = () => this.call<{ auth_required: boolean; principal: Principal | null }>("/api/auth/me");
  login = (name: string, password: string) =>
    this.call<{ user: HouseholdUser; token: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ name, password }),
    });
  logout = () => this.call<{ ok: true }>("/api/auth/logout", { method: "POST" });
  changePassword = (current: string, next: string) =>
    this.call<{ ok: true }>("/api/auth/password", { method: "POST", body: JSON.stringify({ current, new: next }) });
  users = () => this.call<HouseholdUser[]>("/api/household/users");
  addUser = (body: { name: string; password: string; role: string; instances: string[] }) =>
    this.call<HouseholdUser>("/api/household/users", { method: "POST", body: JSON.stringify(body) });
  removeUser = (name: string) =>
    this.call<{ ok: true }>(`/api/household/users/${encodeURIComponent(name)}/remove`, { method: "POST" });
  addMandant = (type: string, title: string) =>
    this.call<Member>("/api/household/mandants", { method: "POST", body: JSON.stringify({ type, title }) });
}

export interface Member {
  id: string;
  title: string;
  type: "private" | "company" | "mailroom" | string;
  url: string;
}

export interface InstanceInfo {
  id: string;
  title: string;
  type: string;
  version: string;
  languages: string[];
  default_language: string;
  model_configured: boolean;
  mail_configured: boolean;
  templates: string[];
  apps: string[];
  open_gates: number;
  country?: string | null;
  capabilities?: { ocr: string | null; transcription: string | null; ffmpeg: boolean };
}

export type Multilingual = string | Record<string, string>;

export interface Gate {
  case_id: string;
  step_id: string;
  started_at: string;
  deadline_at: string | null;
  template_id: string;
  template_version: number;
  gate_kind: "generic" | "categorization" | "addressing";
  outcomes: string[];
  options: { id: string; title: string; titles?: Record<string, string> }[];
  suggested: string | string[] | null;
  template_title: string;
  template_titles: Record<string, string>;
  app: string | null;
  instance: string;
  document: Record<string, string | boolean> | null;
  /** Notification stages reached for this gate: kind → first time. */
  notified?: Record<string, string>;
  /** A hand-set follow-up carries its own subject and note. */
  subject?: string | null;
  note?: string | null;
  /** What the pipeline produced before this gate (ADR-0041 §4). */
  results?: Record<string, Record<string, unknown>>;
  media?: { name: string; bytes: number; kind: string }[];
  stub_steps?: string[];
}

export interface CaseRow {
  id: string;
  template_id: string;
  template_version: number;
  status: "running" | "completed" | "failed" | "cancelled";
  current_step_id: string | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  template_title: string;
  template_titles: Record<string, string>;
  app: string | null;
  instance: string;
  document: Record<string, string | boolean> | null;
}

export interface StepRow {
  step_id: string;
  kind: string;
  status: string;
  outcome: string | null;
  result: unknown;
  model_ref: string | null;
  deadline_at: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface AuditRow {
  seq: number;
  at: string;
  actor_type: string;
  actor_id: string;
  event: string;
  details: Record<string, unknown> | null;
  app_version: string;
  model_ref: string | null;
}

export interface CaseLink {
  id: string;
  template_id: string;
  status: string;
  template_titles?: Multilingual;
}

export interface CaseDetail extends CaseRow {
  summary: Record<string, unknown> | null;
  parent?: CaseLink | null;
  children?: CaseLink[];
  steps: StepRow[];
  audit: AuditRow[];
  results?: Record<string, Record<string, unknown>>;
  media?: { name: string; bytes: number; kind: string }[];
  stub_steps?: string[];
}

/** Text preview of a case's document (first PREVIEW_CHARS of the text layer). */
export interface DocumentText {
  name: string;
  text: string | null;
  truncated: boolean;
  pending?: boolean; // a recording the processing tick has not transcribed yet
  error?: string;
  page_count?: number;
  method?: string;
}

export interface AppSetting {
  name: string;
  type: "principal" | "amount" | "duration" | "choice" | "text" | "bool";
  label: Record<string, string>;
  help: Record<string, string>;
  required: boolean;
  default: unknown;
  choices: string[];
  binds: string[];
}

export interface TrustInfo {
  level: "local" | "official" | "verified" | "community";
  verified: boolean;
  reason: string;
  signed_by: string;
}

export interface CatalogEntry {
  id: string;
  version: string;
  name: Multilingual;
  description: Multilingual;
  publisher: Record<string, string>;
  trust_level: "official" | "verified" | "community";
  requires: Record<string, unknown>;
  model_roles: string[];
  dependencies: Record<string, string>;
  size: number;
  installed_version: string | null;
  state: "installed" | "update" | "available";
  installable: boolean;
  revoked: { id: string; version?: string; reason?: string } | null;
}

export interface CatalogView {
  url: string;
  generated_at: string | null;
  signed_by: string | null;
  error: string | null;
  trusted_levels: string[];
  entries: CatalogEntry[];
  revoked_installed: { id: string; version?: string; reason?: string; installed_version: string | null }[];
  fetched_at: string | null;
}

export interface InstallResult {
  ok: true;
  id: string;
  version: string;
  replaced_version: string | null;
  trust: TrustInfo;
}

export interface AppRow {
  id: string;
  version: string;
  format: number;
  trust?: TrustInfo;
  publisher?: Record<string, string>;
  enabled: boolean;
  loaded: boolean;
  installed_at: string;
  updated_at: string;
  settings_values: Record<string, unknown>;
  name?: Record<string, string>;
  description?: Record<string, string>;
  license?: string;
  author?: Record<string, string>;
  requires?: { core: string; data_class: string; connectors: string[]; guardrails: string[] };
  settings?: AppSetting[];
  templates?: { id: string; title: Multilingual; steps: number }[];
  doctypes?: { id: string; title: Record<string, string>; chain: string }[];
  tests?: string[];
}

export interface DoctypeRow {
  id: string;
  title: Record<string, string>;
  description: Record<string, string>;
  features: string[];
  chain: string;
  fields: Record<string, string>;
  source: string; // app id | "local" | "core"
  overrides: string | null;
}

/** A declared record (ADR-0026): schema fields resolved for the stored
 * variant, plus the current values and how complete they are. */
export interface RecordField {
  name: string;
  type: "text" | "choice" | "bool" | "number" | "email";
  label: Record<string, string>;
  help: Record<string, string>;
  required: boolean;
  choices: string[];
  choice_labels: Record<string, Record<string, string>>;
  pattern: string | null;
  example: string;
  default: unknown;
  group: string;
}

export interface RecordCompleteness {
  required: number;
  filled: number;
  complete: boolean;
}

export interface RecordMeta {
  version: number;
  saved_at: string;
  saved_by: string;
}

export interface RecordRow {
  id: string;
  title: Record<string, string>;
  description: Record<string, string>;
  scope: "any" | "company" | "private";
  kind: "single" | "collection";
  source: string;
  completeness: RecordCompleteness | null;
  meta: RecordMeta | null;
  rows?: number;
}

/** One row of a collection (contacts, accounts): versioned like a record. */
export interface CollectionRow {
  row_id: string;
  version: number;
  label: string;
  values: Record<string, unknown>;
  saved_at: string;
  saved_by: string;
  deleted: boolean;
  history?: { version: number; saved_at: string; saved_by: string; deleted: boolean; values: Record<string, unknown> }[];
}

export interface RecordDetail extends RecordRow {
  label_field: string | null;
  search_fields: string[];
  variant_field: string | null;
  variants: string[];
  fields: RecordField[];
  common_fields: RecordField[];
  variant_fields: Record<string, RecordField[]>;
  values: Record<string, unknown>;
}

/** Time as a trigger (Stufe 1.3). */
export interface ScheduleRow {
  template_id: string;
  title?: Multilingual;
  app?: string | null;
  every?: "day" | "week" | "month";
  at?: string;
  weekday?: number | null;
  day?: number | null;
  next_at?: string | null;
  last_fired_at?: string | null;
  error?: string;
}

export interface DeadlineRow {
  case_id: string;
  step_id: string;
  deadline_at: string;
  started_at: string;
  template_id: string;
  template_title: Multilingual;
  title: string | null;
  note: string | null;
  about_case: string | null;
}

/** Every way a document reaches an instance (Einstellungen → Kanäle). */
export interface AddresseeRow {
  id: string;
  title: string;
  channel: string;
  path: string;
  catch_all: boolean;
}

export interface MailChannel {
  imap_host: string;
  imap_user: string;
  imap_folder: string;
  password: "env" | "file" | "none";
  active: boolean;
}

export interface NotifySettings {
  notify_to: string;
  escalate_to: string;
  remind_after_hours: number;
  escalate_after_hours: number;
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  smtp_from: string;
  smtp_security: "starttls" | "ssl" | "none";
  public_url: string;
  password: "env" | "file" | "none";
  active: boolean;
}

export interface NotificationRow {
  id: string;
  at: string;
  kind: "new" | "reminder" | "escalation";
  case_id: string;
  step_id: string;
  recipient: string;
  subject: string;
  status: "pending" | "sent" | "failed" | "unsent";
  attempts: number;
  sent_at: string | null;
  error: string | null;
}

export interface ChannelsInfo {
  folder: { inbox_dir: string };
  mail: MailChannel;
  notifications: NotifySettings;
  web: { token: "env" | "file" | "none"; endpoint: string };
  upload: { enabled: boolean };
  addressees: AddresseeRow[];
  mailroom: boolean;
}

export interface TemplateRow {
  id: string;
  title: Record<string, string>;
  app: string | null;
  steps: number;
  intake: boolean;
}

export interface TemplateEdge {
  outcome: string;
  kind: "step" | "process" | "end";
  target: string | null;
}

export interface TemplateStep {
  id: string;
  kind: "trigger" | "agent_step" | "human_gate" | "system_action" | "timer";
  config: Record<string, unknown>;
  next: TemplateEdge[];
}

export interface TemplateDetail {
  id: string;
  version: number;
  title: Record<string, string>;
  description: Record<string, string>;
  app: string | null;
  intake: boolean;
  start: string;
  policies: Record<string, Record<string, unknown>>;
  parameters: Record<string, unknown>;
  steps: TemplateStep[];
}

// Workshop: solutions in declaration format 2 (ADR-0035).
export interface SolutionTree {
  where: string;
  processes: string[];
  decisions: string[];
  ai: string[];
  skills: string[];
  tools: string[];
  types: string[];
  knowledge: string[];
  doctypes: string[];
  samples?: string[];
  packages: Record<string, SolutionTree>;
}

// The card canvas's graph of a process (ADR-0040).
export type NodeKind = "start" | "ai" | "gate" | "action" | "decision" | "call" | "timer" | "xor" | "parallel" | "end";

export interface GraphNode {
  id: string;
  kind: NodeKind;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
  ref?: string;
  source?: string;
  /** The way in of a start event (ADR-0057): chat, messenger, … */
  channel?: string;
  seconds?: number;
  deadline?: number | null;
  for_each?: string;
  /** Settings of the step (ADR-0062): attributes + aimn:property of its extension. */
  config?: StepConfig | null;
  /** Multi-instance: run once per element of a list (ADR-0037). */
  loop?: { over: string; as: string; max?: number; collect?: string } | null;
}

export type ConfigValue = string | number | boolean | string[] | { [key: string]: ConfigValue };
export type StepConfig = { [key: string]: ConfigValue };

export interface ContractInput {
  name: string;
  description: string;
  default?: string;
}

// The designer's building blocks (ADR-0062, GET …/designer/catalog).
export interface SettingSpec {
  type?: "bool" | "int" | "text" | "time" | "choice" | "collection" | "space";
  label?: string;
  required?: boolean;
  multiline?: boolean;
  default?: string | number | boolean;
  choices?: string[];
}
export interface ActionBlock {
  id: string;
  group: string;
  effect: string;
  config: Record<string, SettingSpec>;
  inputs: Record<string, SettingSpec>;
  inputs_from?: "collection";
  outputs: Record<string, string>;
}
export interface ToolBlock {
  id: string;
  name: string;
  description: string;
  kind: string;
  effect: string;
  inputs: Record<string, SettingSpec>;
  outputs: Record<string, string>;
  on_error: string;
}
export interface DesignerCatalog {
  triggers: { id: string; config: Record<string, SettingSpec> }[];
  actions: ActionBlock[];
  tools: ToolBlock[];
  briefings: { id: string; role: string; model_role: string; outputs: Record<string, string> }[];
  decisions: { id: string; name: string; inputs: string[]; outputs: string[] }[];
  processes: { id: string; title: string; inputs: Record<string, string> }[];
  collections: Record<string, { title: string; fields: { name: string; required: boolean; label: string }[] }>;
  spaces: { id: string; title: string }[];
  constants: string[];
  gate: Record<string, SettingSpec>;
  outcomes: Record<string, string[]>;
}
export interface RunReport {
  case_id: string;
  status: string;
  waiting_on: { step_id: string; kind: string }[];
  outputs: Record<string, { outcome: string; result: unknown }>;
  children: string[];
}

// A condition of a deciding gateway's flow (ADR-0056): a path into the
// case data and an S-FEEL unary test, as in the DMN tables.
export interface EdgeCondition {
  input: string;
  test: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  outcome: string;
  when?: { match: "all" | "any"; conditions: EdgeCondition[] };
  default?: boolean;
  count?: number;
}

export interface ProcessGraph {
  process: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  writable?: boolean;
  /** The start form's fields (contract inputs). */
  contract?: ContractInput[];
}

// Open client tasks (ADR-0041 §4): "deliver this" entries of the inbox.
export interface ClientTask {
  id: string;
  case_id: string;
  step_id: string;
  action: string;
  status?: "open" | "claimed";
  claimed_by?: string | null;
  claimed_at?: string | null;
  created_at: string;
  template_id: string | null;
  template_title: Multilingual;
  app: string | null;
  params: {
    tool?: string;
    name?: Multilingual;
    instruction?: Multilingual;
    expects?: "file" | "data";
    kind?: "image" | "video" | "audio" | "text";
    links?: Record<string, string>;
    inputs?: Record<string, unknown>;
    outputs?: Record<string, string>;
    [key: string]: unknown;
  };
}

export interface NewElementBody {
  id: string;
  title?: Multilingual | string;
  ai?: string;
  model_role?: string;
  source?: "manual" | "chain" | "folder" | "mailbox" | "schedule";
  /** "solution/process": copy that process as the template (UI only) */
  template?: string;
}

/** ADR-0039 stage 1: an import or template copy waiting for release */
export interface DraftMeta {
  id: string;
  solution: string;
  origin: string;
  title: string;
  created_at: string;
  actor: string;
  files: string[];
  changed: string[];
  findings: string[];
  warnings: string[];
  report?: ImportReport | Record<string, never>;
}

/** ADR-0061: an MCP server of the instance - names of secrets, never values. */
export interface McpServerView {
  name: string;
  transport: "http" | "stdio";
  url: string;
  headers: Record<string, string>;
  command: string[];
  env: Record<string, string>;
  tools: string[];
  timeout_seconds: number;
  note: string;
  secrets: string[];
  missing_secrets: string[];
}

export interface McpServerBody {
  url?: string;
  headers?: Record<string, string>;
  command?: string[];
  env?: Record<string, string>;
  tools: string[];
  timeout_seconds?: number;
  note?: string;
}

export interface McpToolInfo {
  name: string;
  title: string;
  description: string;
  input_schema: { properties?: Record<string, { type?: string; description?: string }>; required?: string[] };
  annotations: { readOnlyHint?: boolean; destructiveHint?: boolean; [k: string]: unknown };
  allowed: boolean;
}

/** ADR-0060: what a translation from another platform made of each node. */
export interface ImportReport {
  source: string;
  process: string;
  title: string;
  counts: { translated: number; approximated: number; placeholder: number };
  items: { node: string; type: string; status: "translated" | "approximated" | "placeholder"; step: string; note: string }[];
  secrets: string[];
  inputs: string[];
  model_roles: string[];
}

export interface DraftView extends DraftMeta {
  texts: Record<string, string>;
  graphs: Record<string, ProcessGraph | { error: string }>;
}

export interface NewCaseBody {
  id: string;
  sample: string;
  description?: string;
  decisions?: Record<string, string>;
  status?: "completed" | "running";
  outcomes?: Record<string, string>;
  run?: boolean;
  record?: boolean;
}

export interface SolutionRow {
  id: string;
  version: string;
  format: number;
  name: Multilingual;
  description: Multilingual;
  path: string;
  writable: boolean;
  configurations: string[];
  active_configuration?: string;
  model_roles: Record<string, { max_data_class: string; vision: boolean; structured_output: boolean; model?: string | null; model_ref?: string | null; reason?: string | null; pinned?: string | null }>;
  dependencies: Record<string, string>;
  sidecars: number;
  drafts?: number;
  trust?: TrustInfo;
  tree: SolutionTree;
}

export type ElementKind = "process" | "decision" | "ai" | "skill" | "tool" | "configuration";

export interface ElementStep {
  id: string;
  kind: TemplateStep["kind"];
  outcomes: string[];
  next?: Record<string, string>;
  prompt_hash: string | null;
  briefing: string | null;
  tool: string | null;
  action: string | null;
  decision: string | null;
  // agent steps
  model_role?: string | null;
  flexibility?: string;
  skills?: string[];
  tools?: string[];
  output?: Record<string, string>;
  context?: Record<string, unknown>;
  mechanics?: "classify" | "assign" | null;
  data_class?: string | null;
  // gates
  approving_outcomes?: string[];
  deadline_seconds?: number | null;
  categorization?: boolean;
  // system actions
  effect?: "internal" | "read" | "external";
  mandate?: boolean;
  process?: string | null;
  tool_mode?: "internal" | "live" | "stub" | "log";
  exec?: "client";
  inputs?: Record<string, string>;
  collection?: string | null;
  // timers / triggers
  timer?: Record<string, unknown>;
  source?: string | null;
  // parallel structure (ADR-0037)
  forks?: Record<string, string[]>;
  join_expects?: string[];
  for_each?: { over: string; as: string; collect?: string; max?: number };
}

export interface StepHistory {
  runs: number;
  outcomes: Record<string, number>;
  last_at: string | null;
  last_outcome: string | null;
  last_status: string | null;
  last_error: string | null;
  last_case: string | null;
  open: number;
  instances?: number;
  branches?: number;
}

export interface BriefingFields {
  role: Record<string, string>;
  instructions: Record<string, string>;
  limits: string[];
  model_role: string;
  flexibility: string;
  skills: string[];
  tools: string[];
  output: Record<string, string>;
  confidence_threshold?: number | null;
}

export interface SolutionElement {
  id: string;
  kind: ElementKind;
  file: string;
  text: string;
  summary: Record<string, unknown> & { steps?: ElementStep[]; stats?: Record<string, number>; history?: Record<string, StepHistory>; title?: Multilingual; contract?: { input?: Record<string, string>; output?: Record<string, string>; defaults?: Record<string, unknown> }; source?: string };
  sidecar: string | null;
  writable: boolean;
}

export interface ProvingResult {
  case_id: string;
  subject_kind: string;
  subject_id: string;
  description: string;
  passed: boolean;
  attempts: number;
  failures: string[];
  visited: string[];
  outcomes: Record<string, string>;
  duration_ms: number;
}

export interface ProvingReport {
  ok: boolean;
  solution_id: string;
  solution_version: string;
  configuration: string;
  results: ProvingResult[];
  coverage: Record<string, { steps_total: number; steps_visited: string[]; outcomes_total: number; outcomes_visited: string[]; step_ratio: number }>;
  missing_recordings: string[];
  errors: string[];
  duration_ms: number;
}

export interface ModelRow {
  id: string;
  model: string;
  provider: "anthropic" | "ollama" | "openai";
  base_url: string;
  clearance: string;
  role: string;
  status: string;
  cost_in_chf: number;
  cost_out_chf: number;
  local: boolean;
  region: string;
  dpa: boolean;
  vision: boolean;
  note: string;
  /** Highest data class this deployment may process for the instance's country. */
  permitted_up_to: string;
  missing_secret: string | null;
}

export type ModelBody = Omit<ModelRow, "id" | "permitted_up_to" | "missing_secret">;

export interface Jurisdiction {
  country: string;
  law: string;
  allowed_regions: string[];
  dpa_required_from: string;
}

export interface ModelsContext {
  country: string | null;
  jurisdiction: Jurisdiction | null;
  known: string[];
  providers: string[];
  regions: string[];
  secrets: Record<string, "env" | "file" | "none">;
}

export interface ModelTestResult {
  ok: boolean;
  reason?: "missing_secret" | "model_not_pulled" | "call_failed";
  secret?: string;
  available?: string[];
  detail?: string;
  raw?: string | null;
  model_ref?: string;
}


// Knowledge spaces (ADR-0031).
export interface KnowledgeSpace {
  id: string;
  title: Multilingual;
  description: Multilingual;
  data_class: string;
  owner: string;
  source: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  documents?: number;
  chunks?: number;
}

export interface KnowledgeDocument {
  id: string;
  space: string;
  title: string;
  file_name: string;
  version: number;
  size: number | null;
  status: "indexed" | "failed";
  error: string | null;
  method: string | null;
  chunks: number;
  embedded: boolean;
  uploaded_by: string | null;
  uploaded_at: string;
  deleted: boolean;
  source_id: string | null; // web source that brought it in (ADR-0045)
  url: string | null;
}

export interface KnowledgeHit {
  space: string;
  document: string;
  source: string;
  text: string;
  score: number;
  url?: string;
}

// Web sources of a knowledge space (ADR-0045).
export type KnowledgeSourceScope = "page" | "path" | "site";
export const KNOWLEDGE_DOC_TYPES = ["html", "pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "txt", "md", "csv"] as const;

export interface KnowledgeSourceInput {
  url: string;
  title: string;
  scope: KnowledgeSourceScope;
  doc_types: string[];
  max_pages: number;
  max_depth: number;
  exclude: string[];
  interval_hours: number;
  enabled: boolean;
}

export interface KnowledgeSourceStats {
  found: number;
  new: number;
  updated: number;
  unchanged: number;
  removed: number;
  unreadable: number;
  skipped: number;
  errors: number;
  requests: number;
  complete: boolean;
}

export interface KnowledgeSource extends KnowledgeSourceInput {
  id: string;
  space: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  last_run_at: string | null;
  next_run_at: string | null;
  last_status: "running" | "ok" | "partial" | "failed" | null;
  last_error: string | null;
  last_stats: KnowledgeSourceStats | null;
  documents: number;
  running: boolean;
}

// Key figures (ADR-0029): aggregation over the trails, plus the budget.
export interface BudgetState {
  monthly_budget_chf: number;
  budget_stop: boolean;
  spent_chf: number;
  ratio: number | null;
  state: "none" | "ok" | "warning" | "exceeded";
  stop: boolean;
}

export interface MetricsTemplateRow {
  template_id: string;
  template_title?: Multilingual | null;
  calls: number;
  errors: number;
  cost_chf: number;
  duration_ms: number;
  cases: number;
  cost_per_case_chf: number | null;
}

export interface OperationsOverview {
  days: number;
  since: string;
  now: string;
  cases: { started: number; completed: number; failed: number; running: number; per_template: { template_id: string; template_title?: Multilingual; started: number; completed: number; failed: number }[] };
  running: { case_id: string; template_id: string; template_title?: Multilingual; step_id: string | null; waiting_on: "gate" | "device" | "timer" | "runtime"; since: string; created_at: string }[];
  waiting: Record<string, number>;
  recent: { case_id: string; template_id: string; template_title?: Multilingual; status: string; closed_at: string | null; duration_s: number | null; last_step: string | null; last_outcome: string | null }[];
  schedules: ScheduleRow[];
  client_tasks: { open: number; claimed: number };
  errors_24h: { errors: number; warnings: number; by_event: { level: string; source: string; event: string; count: number }[]; last: { at: string; level: string; source: string; event: string; detail: string; context: Record<string, unknown> }[] };
  tokens: {
    per_day: { day: string; calls: number; tokens_in: number; tokens_out: number; cost_chf: number; errors: number }[];
    per_template: { template_id: string; template_title?: Multilingual; calls: number; tokens_in: number; tokens_out: number; cost_chf: number; cases: number }[];
    window: { calls: number; errors: number; tokens_in: number; tokens_out: number; cost_chf: number };
  };
  budget: BudgetState;
}

export interface MetricsOverview {
  days: number;
  since: string | null;
  llm: {
    calls: number;
    errors: number;
    tokens_in: number;
    tokens_out: number;
    cost_chf: number;
    duration_ms: number;
    per_template: MetricsTemplateRow[];
    per_model_task: { model_ref: string; task: string; calls: number; errors: number; tokens_in: number; tokens_out: number; cost_chf: number; duration_ms: number }[];
    per_data_class: { data_class: string; calls: number; cost_chf: number }[];
  };
  gates: { template_id: string; template_title?: Multilingual | null; gate: string; decisions: number; approved: number; approved_share: number; outcomes: Record<string, number> }[];
  briefings?: {
    template_id: string;
    template_title?: Multilingual | null;
    step: string;
    current?: string | null;
    versions: { prompt_hash: string; first_seen: string; last_seen: string; runs: number; decisions: number; approved: number; approved_share: number | null; models: string[] }[];
  }[];
  cases: {
    started: number;
    completed: number;
    failed: number;
    running: number;
    per_template: { template_id: string; template_title?: Multilingual | null; started: number; completed: number; failed: number }[];
  };
  budget: BudgetState;
  security?: { flagged: number; redacted: number; blocked: number; refused: number; per_guardrail: { guardrail: string; action: string; count: number }[] };
}

export interface LlmRow {
  id: string;
  at: string;
  case_id: string | null;
  step_id: string | null;
  task: string;
  model_ref: string | null;
  data_class: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_chf: number | null;
  duration_ms: number | null;
  status: string;
  error: string | null;
}

export interface LlmDetail extends LlmRow {
  input: unknown;
  response: unknown;
}

export interface DiagRow {
  id: string;
  at: string;
  level: string;
  source: string;
  event: string;
  detail: string;
  context: string | null;
}

export interface MandateRow {
  id: string;
  action: string;
  target: string;
  condition: Record<string, unknown>;
  comment: string;
  granted_by: string;
  granted_at: string;
  revoked_by: string | null;
  revoked_at: string | null;
}

export interface Suggestion {
  action: string;
  target: string;
  condition: Record<string, unknown>;
  count: number;
  reason: string;
  /** gate rules (ADR-0054): the process and gate the rule decides */
  template_id?: string;
  step_id?: string;
  title?: unknown;
}

/** A file handed in from the shell (base64, like the web form channel). */
export interface IntakeFile {
  name: string;
  content_base64: string;
}

/** What the instance answers a chat message with (key + params + data). */
export interface ChatStart {
  process_id: string;
  titles: Multilingual;
  contract: Record<string, string>;
  defaults?: Record<string, unknown>;
  prefill?: Record<string, string>;
}

export interface ChatAnswer {
  intent: "pending" | "cases" | "case" | "help" | "start" | "unknown";
  answer: string;
  params: Record<string, string | number>;
  gates?: Gate[];
  cases?: CaseRow[];
  case?: CaseDetail;
  start?: ChatStart;
  start_options?: ChatStart[];
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export class InstanceClient {
  constructor(public readonly member: Member) {}

  private base(): string {
    // Same-origin member: relative URLs (dev proxy, production /app).
    if (typeof window !== "undefined" && window.location.origin === this.member.url) return "";
    return this.member.url;
  }

  private async call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${this.base()}/api/v1${path}`, {
      ...init,
      credentials: "include", // the household's session cookie (ADR-0025)
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
    if (resp.status === 401) {
      // Session gone: tell the shell once, it shows the login.
      window.dispatchEvent(new CustomEvent("aitmation:unauthorized"));
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = (await resp.json()) as { detail?: unknown };
        if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch {
        /* no body */
      }
      throw new ApiError(detail, resp.status);
    }
    return (await resp.json()) as T;
  }

  instance = () => this.call<InstanceInfo>("/instance");
  household = () =>
    this.call<{ self: string; members: Member[]; launcher: string | null; auth_required?: boolean }>("/household");
  gates = () => this.call<Gate[]>("/gates");
  decide = (caseId: string, stepId: string, body: Record<string, unknown>) =>
    this.call<{ ok: true }>(`/cases/${caseId}/steps/${stepId}/decide`, { method: "POST", body: JSON.stringify(body) });
  cases = (status?: string) => this.call<CaseRow[]>(`/cases${status ? `?status=${status}` : ""}`);
  startTemplate = (id: string, payload: Record<string, unknown>, file?: IntakeFile) =>
    this.call<{ case_id: string }>(`/templates/${encodeURIComponent(id)}/start`, { method: "POST", body: JSON.stringify({ payload, file }) });
  clientTasks = () => this.call<ClientTask[]>("/client-tasks");
  fulfilClientTask = (id: string, body: { file?: IntakeFile; data?: Record<string, unknown> }) =>
    this.call<{ ok: true; result: Record<string, unknown> }>(`/client-tasks/${encodeURIComponent(id)}/fulfil`, { method: "POST", body: JSON.stringify(body) });
  upload = (body: { subject?: string; text?: string; files: IntakeFile[]; language?: string }) =>
    this.call<{ accepted: number; files: string[] }>("/upload", { method: "POST", body: JSON.stringify(body) });
  chat = (message: string) =>
    this.call<ChatAnswer>("/chat", { method: "POST", body: JSON.stringify({ message }) });
  case = (id: string) => this.call<CaseDetail>(`/cases/${id}`);
  /** Absolute URL of the document file; opens in a tab with the session cookie. */
  mediaUrl = (id: string, name: string) => `${this.base()}/api/v1/cases/${id}/media/${encodeURIComponent(name)}`;
  documentUrl = (id: string, index = 0) => `${this.base()}/api/v1/cases/${id}/document${index ? `?index=${index}` : ""}`;
  documentText = (id: string, index = 0) =>
    this.call<DocumentText>(`/cases/${id}/document/text${index ? `?index=${index}` : ""}`);
  catalog = (refresh = false) => this.call<CatalogView>(`/catalog${refresh ? "?refresh=true" : ""}`);
  installFromCatalog = (id: string, version: string) =>
    this.call<InstallResult>("/catalog/install", { method: "POST", body: JSON.stringify({ id, version }) });
  installBundleFile = (name: string, contentBase64: string) =>
    this.call<InstallResult>("/catalog/install-file", { method: "POST", body: JSON.stringify({ name, content_base64: contentBase64 }) });
  solutionExportUrl = (id: string) => `${this.base()}/api/v1/solutions/${id}/export`;
  apps = () => this.call<AppRow[]>("/apps");
  app = (id: string) => this.call<AppRow>(`/apps/${id}`);
  enableApp = (id: string, enabled: boolean) =>
    this.call<{ ok: true }>(`/apps/${id}/enable`, { method: "POST", body: JSON.stringify({ enabled }) });
  saveAppSettings = (id: string, values: Record<string, unknown>) =>
    this.call<{ ok: true }>(`/apps/${id}/settings`, { method: "PUT", body: JSON.stringify({ values }) });
  doctypes = () => this.call<DoctypeRow[]>("/doctypes");
  saveDoctype = (id: string, body: Omit<DoctypeRow, "id" | "source" | "overrides">) =>
    this.call<DoctypeRow>(`/doctypes/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) });
  deleteDoctype = (id: string) =>
    this.call<{ ok: true; fallback: boolean }>(`/doctypes/${encodeURIComponent(id)}`, { method: "DELETE" });
  records = () => this.call<RecordRow[]>("/records");
  record = (id: string) => this.call<RecordDetail>(`/records/${encodeURIComponent(id)}`);
  saveRecord = (id: string, values: Record<string, unknown>) =>
    this.call<RecordDetail>(`/records/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ values }) });
  recordHistory = (id: string) => this.call<RecordMeta[]>(`/records/${encodeURIComponent(id)}/history`);
  rows = (id: string, q = "") => this.call<CollectionRow[]>(`/records/${encodeURIComponent(id)}/rows?q=${encodeURIComponent(q)}`);
  row = (id: string, rowId: string) => this.call<CollectionRow>(`/records/${encodeURIComponent(id)}/rows/${encodeURIComponent(rowId)}`);
  createRow = (id: string, values: Record<string, unknown>) =>
    this.call<CollectionRow>(`/records/${encodeURIComponent(id)}/rows`, { method: "POST", body: JSON.stringify({ values }) });
  saveRow = (id: string, rowId: string, values: Record<string, unknown>) =>
    this.call<CollectionRow>(`/records/${encodeURIComponent(id)}/rows/${encodeURIComponent(rowId)}`, { method: "PUT", body: JSON.stringify({ values }) });
  deleteRow = (id: string, rowId: string) =>
    this.call<{ ok: true }>(`/records/${encodeURIComponent(id)}/rows/${encodeURIComponent(rowId)}`, { method: "DELETE" });
  schedules = () => this.call<ScheduleRow[]>("/schedules");
  runSchedule = (templateId: string) =>
    this.call<{ case_id: string }>(`/schedules/${encodeURIComponent(templateId)}/run`, { method: "POST", body: JSON.stringify({}) });
  deadlines = () => this.call<DeadlineRow[]>("/deadlines");
  createFollowUp = (body: { title: string; due_at: string; note?: string; about_case?: string | null }) =>
    this.call<{ case_id: string }>("/followups", { method: "POST", body: JSON.stringify(body) });
  channels = () => this.call<ChannelsInfo>("/channels");
  saveMail = (body: { imap_host: string; imap_user: string; imap_folder: string; password?: string | null }) =>
    this.call<MailChannel>("/channels/mail", { method: "PUT", body: JSON.stringify(body) });
  testMail = () => this.call<{ ok: boolean; reason?: string; detail?: string }>("/channels/mail/test", { method: "POST" });
  saveNotifications = (body: Omit<NotifySettings, "password" | "active"> & { password?: string | null }) =>
    this.call<NotifySettings>("/channels/notifications", { method: "PUT", body: JSON.stringify(body) });
  testNotifications = (to: string) =>
    this.call<{ ok: boolean; reason?: string; detail?: string }>("/channels/notifications/test", { method: "POST", body: JSON.stringify({ to }) });
  notifications = (limit = 50) => this.call<NotificationRow[]>(`/notifications?limit=${limit}`);
  issueWebToken = () => this.call<{ token: string; endpoint: string; header: string }>("/channels/web/token", { method: "POST" });
  revokeWebToken = () => this.call<{ ok: true }>("/channels/web/token", { method: "DELETE" });
  saveAddressee = (id: string, body: { title: string; path: string; catch_all: boolean }) =>
    this.call<AddresseeRow>(`/addressees/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) });
  deleteAddressee = (id: string) => this.call<{ ok: true }>(`/addressees/${encodeURIComponent(id)}`, { method: "DELETE" });
  solutions = () => this.call<SolutionRow[]>("/solutions");
  pinModelRole = (app: string, role: string, model: string | null) =>
    this.call<Record<string, unknown>>(`/solutions/${encodeURIComponent(app)}/model-roles/${encodeURIComponent(role)}`, { method: "PUT", body: JSON.stringify({ model }) });
  solutionElement = (app: string, kind: ElementKind, id: string) =>
    this.call<SolutionElement>(`/solutions/${encodeURIComponent(app)}/elements/${kind}/${encodeURIComponent(id)}`);
  saveBriefingFields = (app: string, id: string, fields: BriefingFields) =>
    this.call<{ ok: true; warnings: string[] }>(`/solutions/${encodeURIComponent(app)}/elements/ai/${encodeURIComponent(id)}/fields`, { method: "PUT", body: JSON.stringify(fields) });
  saveSolutionElement = (app: string, kind: ElementKind, id: string, text: string) =>
    this.call<{ ok: true; warnings: string[] }>(`/solutions/${encodeURIComponent(app)}/elements/${kind}/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ text }) });
  processGraph = (app: string, process: string) =>
    this.call<ProcessGraph>(`/solutions/${encodeURIComponent(app)}/elements/process/${encodeURIComponent(process)}/graph`);
  designerCatalog = (app: string, lang: string) =>
    this.call<DesignerCatalog>(`/solutions/${encodeURIComponent(app)}/designer/catalog?lang=${encodeURIComponent(lang)}`);
  runProcess = (process: string, payload: Record<string, unknown>, timeout = 60) =>
    this.call<RunReport>(`/templates/${encodeURIComponent(process)}/run`, { method: "POST", body: JSON.stringify({ payload, timeout, actor: "designer" }) });
  saveProcessGraph = (app: string, process: string, graph: { nodes: GraphNode[]; edges: GraphEdge[]; contract?: ContractInput[] }) =>
    this.call<{ ok: true; warnings: string[]; text: string }>(`/solutions/${encodeURIComponent(app)}/elements/process/${encodeURIComponent(process)}/graph`, { method: "PUT", body: JSON.stringify(graph) });
  createSolution = (body: { id: string; name: Multilingual | string; description?: Multilingual | string }) =>
    this.call<SolutionRow>("/solutions", { method: "POST", body: JSON.stringify(body) });
  createSolutionElement = (app: string, kind: "process" | "ai" | "decision", body: NewElementBody) =>
    this.call<{ ok: true; id: string; kind: string; files: string[]; warnings: string[] }>(`/solutions/${encodeURIComponent(app)}/elements/${kind}`, { method: "POST", body: JSON.stringify(body) });
  copyProcess = (app: string, body: { source_solution: string; source_process: string; id: string }) =>
    this.call<{ ok: true; id: string; files: string[]; warnings: string[]; findings?: string[]; draft?: DraftMeta }>(`/solutions/${encodeURIComponent(app)}/elements/process/copy`, { method: "POST", body: JSON.stringify(body) });
  importProcess = (app: string, name: string, text: string) =>
    this.call<{ ok: true; id: string; files: string[]; warnings: string[]; findings?: string[]; draft?: DraftMeta }>(`/solutions/${encodeURIComponent(app)}/elements/process/import`, { method: "POST", body: JSON.stringify({ name, text }) });
  mcpServers = () => this.call<{ stdio_allowed: boolean; servers: McpServerView[] }>("/mcp/servers");
  putMcpServer = (name: string, body: McpServerBody) =>
    this.call<McpServerView>(`/mcp/servers/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) });
  deleteMcpServer = (name: string) => this.call<{ ok: true }>(`/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" });
  mcpTools = (name: string) => this.call<{ server: string; tools: McpToolInfo[] }>(`/mcp/servers/${encodeURIComponent(name)}/tools`, { method: "POST", body: "{}" });
  toolFromMcp = (app: string, server: string, tool: string, id = "") =>
    this.call<{ ok: true; id: string; files: string[]; warnings: string[] }>(`/solutions/${encodeURIComponent(app)}/elements/tool/from-mcp`, { method: "POST", body: JSON.stringify({ server, tool, id }) });
  importForeign = (app: string, text: string, source = "", zipBase64 = "") =>
    this.call<{ ok: true; id: string; files: string[]; warnings: string[]; findings: string[]; draft: DraftMeta; report: ImportReport }>(`/solutions/${encodeURIComponent(app)}/import/foreign`, { method: "POST", body: JSON.stringify({ text, source, zip_base64: zipBase64 }) });
  solutionDrafts = (app: string) => this.call<DraftMeta[]>(`/solutions/${encodeURIComponent(app)}/drafts`);
  draft = (id: string, lang?: string) => this.call<DraftView>(`/drafts/${encodeURIComponent(id)}${lang ? `?lang=${lang}` : ""}`);
  checkDraft = (id: string) => this.call<DraftMeta>(`/drafts/${encodeURIComponent(id)}/check`, { method: "POST" });
  releaseDraft = (id: string) => this.call<{ ok: true; solution: string; files: string[]; warnings: string[] }>(`/drafts/${encodeURIComponent(id)}/release`, { method: "POST", body: "{}" });
  discardDraft = (id: string) => this.call<{ ok: true }>(`/drafts/${encodeURIComponent(id)}`, { method: "DELETE" });
  createSolutionCase = (app: string, process: string, body: NewCaseBody) =>
    this.call<{ ok: true; id: string; sidecar: string; run?: ProvingReport }>(`/solutions/${encodeURIComponent(app)}/elements/process/${encodeURIComponent(process)}/cases`, { method: "POST", body: JSON.stringify(body) });
  proveSolution = (app: string, body: { configuration?: string; only?: string | null; repeat?: number | null; record?: boolean }) =>
    this.call<ProvingReport>(`/solutions/${encodeURIComponent(app)}/test`, { method: "POST", body: JSON.stringify(body) });
  templates = () => this.call<TemplateRow[]>("/templates");
  template = (id: string) => this.call<TemplateDetail>(`/templates/${encodeURIComponent(id)}`);
  models = () => this.call<ModelRow[]>("/models");
  modelsContext = () => this.call<ModelsContext>("/models/jurisdiction");
  saveModel = (id: string, body: ModelBody) =>
    this.call<ModelRow>(`/models/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) });
  deleteModel = (id: string) => this.call<{ ok: true }>(`/models/${encodeURIComponent(id)}`, { method: "DELETE" });
  testModel = (id: string) => this.call<ModelTestResult>(`/models/${encodeURIComponent(id)}/test`, { method: "POST" });
  secrets = () => this.call<Record<string, "env" | "file" | "none">>("/secrets");
  setSecret = (name: string, value: string) =>
    this.call<Record<string, "env" | "file" | "none">>(`/secrets/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ value }) });
  knowledge = () => this.call<{ spaces: KnowledgeSpace[]; embeddings: string | null }>("/knowledge");
  knowledgeSpace = (id: string) => this.call<KnowledgeSpace & { documents: KnowledgeDocument[]; sources: KnowledgeSource[] }>(`/knowledge/${encodeURIComponent(id)}`);
  knowledgeSources = (space: string) => this.call<KnowledgeSource[]>(`/knowledge/${encodeURIComponent(space)}/sources`);
  addKnowledgeSource = (space: string, body: KnowledgeSourceInput) =>
    this.call<KnowledgeSource>(`/knowledge/${encodeURIComponent(space)}/sources`, { method: "POST", body: JSON.stringify(body) });
  saveKnowledgeSource = (space: string, id: string, body: KnowledgeSourceInput) =>
    this.call<KnowledgeSource>(`/knowledge/${encodeURIComponent(space)}/sources/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) });
  deleteKnowledgeSource = (space: string, id: string) =>
    this.call<{ ok: true; documents_removed: number }>(`/knowledge/${encodeURIComponent(space)}/sources/${encodeURIComponent(id)}`, { method: "DELETE" });
  runKnowledgeSource = (space: string, id: string) =>
    this.call<KnowledgeSource>(`/knowledge/${encodeURIComponent(space)}/sources/${encodeURIComponent(id)}/run`, { method: "POST" });
  saveKnowledgeSpace = (id: string, body: { title: Multilingual; description?: Multilingual | null; data_class: string; owner: string }) =>
    this.call<KnowledgeSpace>(`/knowledge/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) });
  archiveKnowledgeSpace = (id: string) => this.call<{ ok: true }>(`/knowledge/${encodeURIComponent(id)}`, { method: "DELETE" });
  addKnowledgeDocument = (space: string, body: { name: string; content_base64: string; title?: string; replace?: string | null }) =>
    this.call<KnowledgeDocument>(`/knowledge/${encodeURIComponent(space)}/documents`, { method: "POST", body: JSON.stringify(body) });
  deleteKnowledgeDocument = (space: string, id: string) =>
    this.call<{ ok: true }>(`/knowledge/${encodeURIComponent(space)}/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
  knowledgeDocumentUrl = (space: string, id: string) =>
    `${this.base()}/api/v1/knowledge/${encodeURIComponent(space)}/documents/${encodeURIComponent(id)}/file`;
  searchKnowledge = (space: string, q: string) =>
    this.call<KnowledgeHit[]>(`/knowledge/${encodeURIComponent(space)}/search?q=${encodeURIComponent(q)}`);
  metrics = (days: number) => this.call<MetricsOverview>(`/metrics?days=${days}`);
  operations = (days: number) => this.call<OperationsOverview>(`/operations?days=${days}`);
  saveBudget = (body: { monthly_budget_chf: number; budget_stop: boolean }) =>
    this.call<BudgetState>("/metrics/budget", { method: "PUT", body: JSON.stringify(body) });
  llmLog = () => this.call<LlmRow[]>("/log/llm");
  llmEntry = (id: string) => this.call<LlmDetail>(`/log/llm/${encodeURIComponent(id)}`);
  diagnostics = () => this.call<DiagRow[]>("/log/diagnostics");
  mandates = () => this.call<MandateRow[]>("/mandates");
  suggestions = () => this.call<Suggestion[]>("/mandates/suggestions");
  grant = (body: { action?: string; target: string; condition: Record<string, unknown>; comment?: string }) =>
    this.call<{ id: string }>("/mandates", { method: "POST", body: JSON.stringify(body) });
  revoke = (id: string) => this.call<{ ok: true }>(`/mandates/${id}/revoke`, { method: "POST" });
}

/** The member that served this page: relative API, discovered household. */
export function selfMember(): Member {
  return { id: "self", title: "aitmation", type: "company", url: window.location.origin };
}
