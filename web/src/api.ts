export type Kind = "fact" | "preference" | "ongoing" | "instruction" | "event";

export interface Topic {
  slug: string;
  title: string;
  description: string | null;
  group: string;
  group_name: string;
  private: boolean;
  count: number;
  sections: string[];
}

export interface Group {
  id: string;
  name: string;
  topics: string[];
}

export interface State {
  version: string;
  vault: string;
  kinds: Kind[];
  topics: Topic[];
  groups: Group[];
  counts: { active: number; archived: number; pending: number; unreviewed: number; review_due: number };
  settings: { require_approval: boolean; onboarded: boolean };
  mcp_url: string;
}

export interface Memory {
  id: string;
  content: string;
  topic: string;
  section: string | null;
  kind: Kind;
  source: string;
  /** Who wrote the latest version: user, file, import:<source> or connector:<id>. */
  last_actor: string;
  created_at: string;
  updated_at: string;
  version: number;
  status: "active" | "archived";
  pinned: boolean;
  private: boolean;
  effective_private: boolean;
  until: string | null;
  review_due: boolean;
  evidence: string | null;
  archived_at: string | null;
}

export type ProposalStatus =
  | "pending" | "accepted" | "edited_accepted" | "rejected" | "expired" | "applied" | "undone";

/** Why a pending proposal waits for the user. sensitive: the Assistant's check flagged it;
 * review: the proposer asked for a decision, e.g. a tidy-up's "possibly outdated" archive. */
export type Hold = "approval" | "protected" | "private" | "sensitive" | "review";

export interface Proposal {
  id: string;
  connector_id: string;
  connector_name: string;
  op: "add" | "update" | "archive";
  content: string | null;
  topic: string | null;
  memory_id: string | null;
  kind: Kind | null;
  reason: string | null;
  evidence: string | null;
  created_at: string;
  status: ProposalStatus;
  hold: Hold | null;
  decided_at: string | null;
  /** When the user saw an applied change; applied without it = still in the digest. */
  reviewed_at: string | null;
  final_content: string | null;
  final_topic: string | null;
  applied_memory_id: string | null;
  applied_version: number | null;
  before_content: string | null;
  before_topic: string | null;
  original_op: string | null;
  similarity: number | null;
  conflict: boolean;
  /** "assistant" when the OpenMyMemo Assistant checked it before it was filed, with what it found. */
  checked_by?: "assistant" | null;
  check_note?: string | null;
  /** A review date the change sets, and the one it replaced. */
  until?: string | null;
  before_until?: string | null;
  /** Proposals one run filed together, e.g. "tidy-2026-10-25-1a2b" for a tidy-up. */
  batch_id?: string | null;
  /** Computed by the server: the change is still the memory's latest version. */
  can_undo: boolean;
}

/** Tidy up (src/openmymemo/assistant/tidy.py): the schedule on this computer and whether it can run. */
export interface TidyStatus {
  enabled: boolean;
  available: boolean;
  reason: string | null;
  max_calls: number;
  schedule: "off" | "daily" | "weekly" | "monthly";
  every_days: number | null;
  last_run: string | null;
  next_run: string | null;
  due: boolean;
}

export interface TidyItem {
  kind: "merge" | "contradiction" | "outdated" | "until" | "rewrite";
  memory_ids: string[];
  reason: string;
  content: string | null;
  until: string | null;
  before: string[];
  filed: { status?: string; proposal_id?: string }[];
  error: string | null;
}

export interface TidyResult {
  ok: boolean;
  message: string;
  dry_run: boolean;
  batch_id: string | null;
  memories: number;
  left_out: number;
  chunks: number;
  calls: number;
  estimate: { calls: number; tokens: number };
  candidates: [string, string][];
  items: TidyItem[];
  applied: number;
  waiting: number;
  seen_before: number;
  stopped: "unavailable" | "busy" | "limit" | "cap" | "error" | null;
}

export interface SortRow {
  memory_id: string;
  content: string;
  from_topic: string;
  /** Suggested topic, or null when nothing matched. */
  topic: string | null;
  confidence: number;
  /** Who suggested it: the OpenMyMemo Assistant or the rules (absent when the Assistant is off). */
  by?: "assistant" | "rules" | null;
}

export interface Connector {
  id: string;
  name: string;
  /** cloud: a web app reaching the folder through OpenMyMemo Cloud (cloud-chatgpt, cloud-claude, …); assistant: the
   * OpenMyMemo Assistant (id "assistant"). */
  kind: "stdio" | "http" | "cloud" | "assistant";
  read_topics: string[] | "all";
  include_private: boolean;
  write: "none" | "propose" | "auto";
  blocked_topics: string[];
  token_preview: string | null;
  has_token: boolean;
  created_at: string;
  last_seen: string | null;
  reads: number;
  proposals: number;
}

/** An AI app from the catalog (src/openmymemo/clients.py) as it stands on this computer. */
export interface AppStatus {
  id: string;
  name: string;
  /**
   * file: OpenMyMemo edits its settings; cli: it runs the app's own command; manual: the person pastes a snippet;
   * account: OpenMyMemo Cloud, a sign-in rather than an app (see CloudStatus).
   */
  method: "file" | "cli" | "manual" | "account";
  /** One line about what it is, where the name alone doesn't say. */
  description?: string | null;
  docs: string;
  site: string | null;
  note: string | null;
  /** A command-line app: "start a new session" rather than "restart". */
  session: boolean;
  /** What to restart (or start a new session of) after connecting, e.g. "Codex or the ChatGPT app". */
  restart_name: string;
  installed: boolean;
  connected: boolean;
  via: "config" | "plugin" | "connector" | "account" | null;
  /** Our plugin in that app, e.g. "the OpenMyMemo extension", when that is how it is connected. */
  plugin: string | null;
  /** Its settings start OpenMyMemo from a path that no longer exists; connecting again fixes them. */
  broken: boolean;
  /** The person removed the app's access; its settings may still be there. */
  revoked: boolean;
  in_use: boolean;
  last_seen: string | null;
  has_connector: boolean;
  connector: Connector | null;
}

export interface AppCatalog {
  /** The server's platform: darwin, linux or win32. */
  system: string;
  apps: AppStatus[];
  /** Connectors that aren't one of the catalog's apps (added by name). */
  custom: Connector[];
  /** The OpenMyMemo Assistant's connector, once it has been turned on (absent from older servers). */
  assistant?: Connector | null;
}

export interface InstallLink {
  label: string;
  url: string;
}

export interface AppSetup {
  id: string;
  name: string;
  snippet: string;
  /** The file the snippet goes into, when there is one. */
  path: string | null;
  /** Where in the app to paste it, when it isn't a file. */
  where: string | null;
  docs: string;
  link: InstallLink | null;
}

export interface ConnectResult {
  /** done: nothing left but a restart; manual: the person finishes it with the snippet; error: it failed. */
  status: "done" | "manual" | "error";
  message: string;
  changed: boolean;
  restart: boolean;
  path: string | null;
  snippet: string | null;
  link: InstallLink | null;
  steps: string[];
}

/** The MyMemo service's plan as it was at linking (and at each connection). */
export interface CloudPlan {
  tier?: string;
  /** Live relay: calls reach this computer while it is online. */
  live?: boolean;
  /** Offline queue: writes wait, encrypted, while it is offline. */
  queue?: boolean;
}

/** A web app using OpenMyMemo Cloud: a connector of its own, or a tombstone once its access was removed. */
export interface CloudApp {
  id: string;
  name: string;
  revoked: boolean;
  last_seen: string | null;
  connector: Connector | null;
}

/** This computer's OpenMyMemo Cloud preferences (product-design.md §16.11). */
export interface CloudSettings {
  /** Listed on the Connectors page (it always is while this computer is linked). */
  show: boolean;
  /** The chosen service; "" for MyMemo's own (`default_service`). */
  service: string;
  default_service: string;
  /** OPENMYMEMO_CLOUD_URL decides the service, when set. */
  service_from_env: string | null;
}

/** OpenMyMemo Cloud on this computer (GET /api/cloud). */
export interface CloudStatus {
  linked: boolean;
  /** Listed: turned on in Settings, or linked. */
  shown: boolean;
  settings: CloudSettings;
  /** The MyMemo service, and the URL ChatGPT and claude.ai need, exactly. */
  service: string;
  endpoint: string;
  /** A sign-in was started here and the browser hasn't come back yet. */
  pending: boolean;
  /** Why the last sign-in didn't link this computer. */
  link_error: string | null;
  folder: string | null;
  /** The link serves the folder this panel shows. */
  this_folder: boolean;
  /** Who this computer is linked to: MyMemo's display name and masked email ("s***@example.com"). */
  account: { display_name: string; email_masked: string } | null;
  plan: CloudPlan | null;
  linked_at: string | null;
  state: "connecting" | "online" | "waiting" | "stopped";
  /**
   * offline (reconnecting) · refused (turned away, but MyMemo didn't say the link is gone; retrying) · plan (the
   * plan doesn't include it; checked hourly) · replaced (another OpenMyMemo took
   * over) · key (link again) · unlinked · closed · folder (linked to another folder) · not_running.
   */
  reason: string | null;
  online: boolean;
  close_code: number | null;
  last_call_at: string | null;
  /** Proposals waiting in OpenMyMemo Cloud's encrypted queue, as of the last connection; null unless queue_enabled. */
  queued: number | null;
  /** Whether this OpenMyMemo takes the offline queue (it asked for it in `hello`). False since the launch, which offers
   * the live relay only: then nothing about the queue is shown, whatever `plan.queue` says. */
  queue_enabled: boolean;
  apps: CloudApp[];
}

export interface ConnectorCreated {
  connector: Connector;
  token: string | null;
  snippets: Record<string, string>;
}

export interface Activity {
  at: string;
  connector: string;
  /** link: this computer was linked to OpenMyMemo Cloud (connector "mymemo-cloud"), to `account` / `email_masked`. */
  /** call: the OpenMyMemo Assistant asked its model (connector "assistant"): feature, model, endpoint host, tokens. */
  action: "context" | "search" | "get" | "propose" | "import" | "link" | "call";
  feature?: string;
  model?: string;
  endpoint?: string;
  tokens_in?: number;
  tokens_out?: number;
  query?: string;
  memory_ids?: string[];
  proposal_id?: string;
  op?: string;
  result?: string;
  chars?: number;
  source?: string;
  account?: string;
  email_masked?: string;
}

export interface HistoryRow {
  at: string;
  memory_id: string;
  version: number;
  op: "create" | "update" | "move" | "archive" | "restore";
  content: string;
  topic: string;
  actor: string;
  proposal_id?: string;
}

export interface ContextResult {
  text: string;
  memory_ids: string[];
  omitted: number;
  chars: number;
  approx_tokens: number;
}

export interface ImportItem {
  content: string;
  /** An existing topic the source names explicitly; null lands in from-<source>. */
  topic: string | null;
  kind: Kind | null;
  created_at: string | null;
  hint: string | null;
  duplicate_of: string | null;
  similar_to: string | null;
  similar_content: string | null;
  include: boolean;
  /** Who split it: the OpenMyMemo Assistant, or the rules when it was on but couldn't be used (null: it was off). */
  by?: "assistant" | "rules" | null;
  /** "dropped": the Assistant left this line out where the rules would have kept it; "sensitive": read from a
   * chat, it looks like health, money or a secret. */
  flag?: "dropped" | "sensitive" | null;
  /** From a chat: the person's quoted message and its date. */
  evidence?: string | null;
}

/** Chat exports and agent sessions, read by the Assistant (src/openmymemo/assistant/chats.py). */
export type ChatSource = "chatgpt-export" | "claude-export" | "claude-code-sessions" | "codex-sessions";

/** A chat import: what was found, what reading it costs, then its progress and preview. */
export interface ChatJob {
  job: string;
  state: "planned" | "running" | "done" | "cancelled" | "failed";
  source: ChatSource;
  conversations: number;
  messages: number;
  newest: string | null;
  oldest: string | null;
  estimate: {
    calls: number;
    tokens: number;
    tokens_in: number;
    tokens_out: number;
    messages: number;
    calls_left?: number | null;
    tokens_left?: number | null;
    remote?: boolean;
    host?: string;
  };
  ready: boolean;
  reason: string | null;
  done: number;
  total: number;
  failed: number;
  error: string | null;
  items?: ImportItem[];
}

export type SessionApp = "claude-code" | "codex";
/** What the person said about the Assistant reading an app's sessions; null: not asked yet. */
export type SessionConsent = "allowed" | "declined" | "asked" | null;

/** A memory or instructions file another AI app keeps on disk (memfiles.py). */
export interface MemoryFile {
  path: string;
  name: string;
  /** The import source it reads as: claude-code, codex, gemini-cli, cursor, windsurf, cline, copilot, kiro, file, json. */
  source: string;
  /** Who keeps it, for people: "Claude Code", "Memory file". */
  app: string;
  size: number;
  modified: number;
}

export interface MemoryFiles {
  path: string;
  home: string;
  files: MemoryFile[];
}

export interface FilePreview {
  source: string;
  name: string;
  items: ImportItem[];
}

export type AssistantFeature = "import" | "sort" | "recall" | "check" | "session" | "tidy";
export type AssistantKind = "local" | "key" | "mymemo";

/** Settings → Assistant (src/openmymemo/assistant/core.py `status`). The key itself is never sent back. */
export interface AssistantStatus {
  enabled: boolean;
  kind: AssistantKind;
  protocol: "openai" | "anthropic";
  /** As set; empty means the kind's default, which `endpoint` shows. */
  base_url: string;
  endpoint: string;
  host: string;
  model: string;
  /** An embeddings model at the same endpoint, for search by meaning; "" for none. */
  embeddings_model: string;
  /** Whether that endpoint's embeddings are used (OpenAI-compatible, on this computer or with a key). */
  embeddings: boolean;
  has_key: boolean;
  key_store: "keychain" | "file" | "env" | null;
  features: Record<AssistantFeature, boolean>;
  private_topics: string[];
  caps: { calls: number | null; tokens: number | null };
  usage: { month: string; calls: number; tokens: number };
  timeout: number;
  /** Seconds an app's proposal may wait for the Assistant's check. */
  check_budget: number;
  session_apps: Record<SessionApp, SessionConsent>;
  /** Fields an OPENMYMEMO_ASSISTANT_* variable sets on this computer: shown, not editable. */
  from_env: string[];
  local: boolean;
  problem: string | null;
  ready: boolean;
  reason: string | null;
  /** Where memory text goes with these settings, in one sentence. */
  where: string;
  connector_id: string | null;
  revoked: boolean;
  /** Which choices can be picked. */
  kinds: Record<AssistantKind, boolean>;
  /** MyMemo's hosted model (product-design.md §17.8), when it is the chosen kind; never a token. */
  mymemo?: AssistantMyMemo | null;
}

/** The service's reason to pause MyMemo's hosted model: features use their rules until `until`. */
export interface AssistantHold {
  code: "plan_required" | "quota_exceeded" | "rate_limited" | "unavailable";
  message: string;
  plan_url: string | null;
  resets_at: string | null;
  until: string;
}

export interface AssistantMyMemo {
  signed_in: boolean;
  service: string;
  /** cloud: the token came from this computer's OpenMyMemo Cloud link; browser: a sign-in in the browser; env: OPENMYMEMO_ASSISTANT_API_KEY. */
  via: "cloud" | "browser" | "env" | null;
  /** The MyMemo account, when known (from the OpenMyMemo Cloud link): its name and masked email. */
  account: { display_name?: string; email_masked?: string } | null;
  signed_in_at: string | null;
  /** "expired": MyMemo stopped accepting the sign-in, so this computer signed out. */
  signed_out: string | null;
  cloud_linked: boolean;
  hold: AssistantHold | null;
  /** A browser sign-in started in this panel is waiting for the browser to come back. */
  pending?: boolean;
  signin_error?: string | null;
}

/** MyMemo's answer about its hosted model, the plan and this period's usage (`GET /models`), as it gave it. */
export type AssistantAccount =
  | {
      ok: true;
      model: string | null;
      display_name: string | null;
      provider: string | null;
      plan: {
        status: string | null; active: boolean; period_start: string | null; period_end: string | null;
        resets_at: string | null; cancel_at_period_end: boolean; plan_url: string | null;
      };
      usage: {
        input_tokens: number | null; output_tokens: number | null; input_tokens_included: number | null;
        output_tokens_included: number | null; requests: number | null;
        /** This month's use in US dollars and what the plan includes (null: no cap); both null from an older
         * service, which reports tokens only. */
        cost_usd: number | null; cost_usd_included: number | null;
        /** The plan's regular monthly use in dollars. A limited-time offer is on while cost_usd_included is more. */
        cost_usd_regular_included: number | null;
        /** When that offer ends, if announced (ISO 8601, UTC). */
        offer_ends_at: string | null;
      };
      limits: { requests_per_minute: number | null; requests_per_day: number | null };
    }
  | { ok: false; error: string; signed_out: boolean };

export interface AssistantPatch {
  enabled?: boolean;
  kind?: AssistantKind;
  protocol?: "openai" | "anthropic";
  base_url?: string;
  model?: string;
  embeddings_model?: string;
  api_key?: string;
  features?: Partial<Record<AssistantFeature, boolean>>;
  private_topics?: string[];
  caps?: { calls?: number | null; tokens?: number | null };
  check_budget?: number;
}

/** One choice under "Search by meaning on this computer" (src/openmymemo/recall/local.py `models_info`). */
export interface SemanticModel {
  id: string;
  name: string;
  size_mb: number;
  dims: number;
  languages: string;
  license: string;
  license_url: string;
  /** Terms the person must accept before the download (EmbeddingGemma's Gemma Terms of Use). */
  terms_url: string | null;
  recommended: boolean;
  note: string;
  /** False until its files are pinned (SHA-256) in this release: it can't be downloaded yet. */
  available: boolean;
  downloaded: boolean;
}

export interface SemanticJob {
  phase: "idle" | "runtime" | "download" | "index" | "done" | "error";
  model: string | null;
  done: number;
  total: number;
  error: string | null;
  code: string | null;
}

/** Settings → Assistant → "Search by meaning on this computer" (src/openmymemo/recall/semantic.py `status`). */
export interface SemanticStatus {
  enabled: boolean;
  model: string;
  accepted_terms: string[];
  ready: boolean;
  models: SemanticModel[];
  runtime: { installed: boolean; command: string | null };
  job: SemanticJob;
  /** What search uses for vectors now: the local model, the Assistant's endpoint, or nothing (keywords). */
  embedder: "local" | "endpoint" | null;
  endpoint: boolean;
  index: { model: string | null; count: number; memories: number; current?: boolean };
}

/** The panel's search box. `keyword`: no vectors, so the panel filters by itself as before. */
export interface SearchResult {
  mode: "keyword" | "hybrid" | "expanded";
  results: { id: string; score: number; match: "exact" | "keyword" | "meaning" | "both" }[];
}

export interface AssistantTest {
  ok: boolean;
  model: string | null;
  ms: number | null;
  error: string | null;
}

const HEADERS = { "X-OpenMyMemo": "1", "Content-Type": "application/json" };

/** A refused request; `status` 409 means the memory changed since the version the request named. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, { method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(typeof data.detail === "string" ? data.detail : res.statusText, res.status);
  }
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  state: () => req<State>("GET", "/api/state"),
  memories: (status: "active" | "archived" = "active") => req<Memory[]>("GET", `/api/memories?status=${status}`),
  addMemory: (body: { topic?: string; content: string; kind?: Kind; section?: string | null; private?: boolean }) =>
    req<Memory>("POST", "/api/memories", body),
  patchMemory: (
    id: string,
    body: Partial<Pick<Memory, "content" | "topic" | "kind" | "pinned" | "private" | "until">> & { expected_version?: number },
  ) => req<Memory>("PATCH", `/api/memories/${enc(id)}`, body),
  archive: (id: string, expectedVersion?: number) =>
    req<Memory>("POST", `/api/memories/${enc(id)}/archive`, expectedVersion === undefined ? undefined : { expected_version: expectedVersion }),
  restore: (id: string) => req<Memory>("POST", `/api/memories/${enc(id)}/restore`),
  rollback: (id: string, version: number) => req<Memory>("POST", `/api/memories/${enc(id)}/rollback`, { version }),
  history: (id: string) => req<HistoryRow[]>("GET", `/api/memories/${enc(id)}/history`),
  addTopic: (body: { slug: string; title: string; description?: string; group?: string; private?: boolean }) =>
    req<Topic>("POST", "/api/topics", body),
  patchTopic: (slug: string, body: { title?: string; description?: string; private?: boolean }) =>
    req<Topic>("PATCH", `/api/topics/${enc(slug)}`, body),
  patchSettings: (body: Partial<State["settings"]>) => req<State["settings"]>("PATCH", "/api/settings", body),
  proposals: (q: { status?: ProposalStatus; unreviewed?: boolean; limit?: number } = {}) => {
    const params = new URLSearchParams();
    if (q.status) params.set("status", q.status);
    if (q.unreviewed) params.set("unreviewed", "true");
    if (q.limit) params.set("limit", String(q.limit));
    const qs = params.toString();
    return req<Proposal[]>("GET", `/api/proposals${qs ? `?${qs}` : ""}`);
  },
  accept: (id: string, body: { content?: string; topic?: string; kind?: Kind; as_new?: boolean } = {}) =>
    req<Proposal>("POST", `/api/proposals/${enc(id)}/accept`, body),
  reject: (id: string, block_topic = false) => req<Proposal>("POST", `/api/proposals/${enc(id)}/reject`, { block_topic }),
  undo: (id: string, block_topic = false) => req<Proposal>("POST", `/api/proposals/${enc(id)}/undo`, { block_topic }),
  /** Keep one applied change: it leaves the digest. */
  reviewed: (id: string) => req<Proposal>("POST", `/api/proposals/${enc(id)}/reviewed`),
  /** "Looks good": every change in the digest is marked as seen. */
  reviewedAll: () => req<{ reviewed: number }>("POST", "/api/proposals/reviewed"),
  /** Keep all / Undo all for a batch (a tidy-up) in the digest. */
  keepBatch: (batch: string) => req<{ reviewed: number }>("POST", `/api/proposals/batches/${enc(batch)}/reviewed`),
  undoBatch: (batch: string) => req<{ undone: number; kept: number }>("POST", `/api/proposals/batches/${enc(batch)}/undo`),
  tidy: () => req<TidyStatus>("GET", "/api/tidy"),
  tidySchedule: (schedule: TidyStatus["schedule"]) => req<TidyStatus>("PATCH", "/api/tidy", { schedule }),
  tidyRun: (body: { dry_run?: boolean; topic?: string | null }) => req<TidyResult>("POST", "/api/tidy", body),
  /** Rewrite an applied change in the user's words; the memory becomes theirs. */
  editApplied: (id: string, content: string, topic?: string) =>
    req<Proposal>("POST", `/api/proposals/${enc(id)}/edit`, { content, topic }),
  connectors: () => req<Connector[]>("GET", "/api/connectors"),
  addConnector: (body: { name: string; kind: "stdio" | "http"; write: Connector["write"] }) =>
    req<ConnectorCreated>("POST", "/api/connectors", body),
  patchConnector: (id: string, body: Partial<Pick<Connector, "name" | "write" | "read_topics" | "include_private" | "blocked_topics">>) =>
    req<Connector>("PATCH", `/api/connectors/${enc(id)}`, body),
  rotate: (id: string) => req<ConnectorCreated>("POST", `/api/connectors/${enc(id)}/rotate`),
  removeConnector: (id: string) => req<{ ok: boolean }>("DELETE", `/api/connectors/${enc(id)}`),
  snippets: (id: string) => req<Record<string, string>>("GET", `/api/connectors/${enc(id)}/snippets`),
  apps: () => req<AppCatalog>("GET", "/api/clients"),
  appSetup: (id: string) => req<AppSetup>("GET", `/api/clients/${enc(id)}/setup`),
  connectApp: (id: string) => req<{ result: ConnectResult; app: AppStatus }>("POST", `/api/clients/${enc(id)}/connect`),
  disconnectApp: (id: string) =>
    req<{ result: ConnectResult; app: AppStatus }>("POST", `/api/clients/${enc(id)}/disconnect`),
  cloud: () => req<CloudStatus>("GET", "/api/cloud"),
  /** Start signing in: returns MyMemo's page to open in the browser. */
  cloudLink: () => req<{ url: string }>("POST", "/api/cloud/link"),
  cloudUnlink: () => req<{ message: string; cloud: CloudStatus }>("DELETE", "/api/cloud"),
  cloudReconnect: () => req<CloudStatus>("POST", "/api/cloud/reconnect"),
  cloudAllow: (id: string) => req<CloudStatus>("POST", `/api/cloud/apps/${enc(id)}/allow`),
  /** Show or hide OpenMyMemo Cloud, or choose its service ("" for MyMemo's own). */
  cloudSettings: (body: { show?: boolean; service?: string }) => req<CloudStatus>("PATCH", "/api/cloud/settings", body),
  assistant: () => req<AssistantStatus>("GET", "/api/assistant"),
  assistantSettings: (body: AssistantPatch) => req<AssistantStatus>("PATCH", "/api/assistant", body),
  /** One tiny request to the configured model: its name and how long it took. */
  assistantTest: () => req<AssistantTest>("POST", "/api/assistant/test"),
  /** Sign in to MyMemo: done at once with this computer's OpenMyMemo Cloud link, else a page to open in the browser. */
  assistantSignIn: (browser = false) =>
    req<{ signed_in: boolean; url: string | null; assistant: AssistantStatus }>("POST", "/api/assistant/mymemo/signin", { browser }),
  assistantAccount: () =>
    req<{ account: AssistantAccount; assistant: AssistantStatus }>("GET", "/api/assistant/mymemo"),
  assistantSignOut: () => req<{ message: string; assistant: AssistantStatus }>("DELETE", "/api/assistant/mymemo"),
  /** Allow or decline reading an app's sessions when they end. */
  assistantSessions: (app: SessionApp, allow: boolean) =>
    req<AssistantStatus>("POST", `/api/assistant/sessions/${enc(app)}`, { allow }),
  semantic: () => req<SemanticStatus>("GET", "/api/semantic"),
  /** Turn search by meaning on (starts the download in the background; poll `semantic`) or off. */
  semanticSet: (body: { enabled: boolean; model?: string; accept_terms?: boolean }) =>
    req<SemanticStatus>("POST", "/api/semantic", body),
  search: (q: string, limit = 200) => req<SearchResult>("GET", `/api/search?q=${enc(q)}&limit=${limit}`),
  context: (connector: string | null, maxChars: number) =>
    req<ContextResult>("GET", `/api/context?max_chars=${maxChars}${connector ? `&connector=${enc(connector)}` : ""}`),
  activity: (connector?: string) =>
    req<Activity[]>("GET", `/api/activity?limit=300${connector ? `&connector=${enc(connector)}` : ""}`),
  importPreview: (source: string, text: string) => req<ImportItem[]>("POST", "/api/import/preview", { source, text }),
  importFiles: (path: string) => req<MemoryFiles>("GET", `/api/import/files?path=${encodeURIComponent(path)}`),
  /** A file OpenMyMemo reads by its path, or one the browser read. */
  importFile: (file: { path: string } | { name: string; text: string }) => req<FilePreview>("POST", "/api/import/file", file),
  /** Read a chat export or sessions (a path in the home folder, or a file the browser read): the cost, no call yet. */
  chatPlan: (body: { source: ChatSource; path?: string; name?: string; text?: string }) =>
    req<ChatJob>("POST", "/api/import/chats", body),
  chatStart: (job: string, maxCalls?: number) =>
    req<ChatJob>("POST", `/api/import/chats/${enc(job)}/start`, maxCalls === undefined ? {} : { max_calls: maxCalls }),
  chatStatus: (job: string) => req<ChatJob>("GET", `/api/import/chats/${enc(job)}`),
  chatCancel: (job: string) => req<{ ok: boolean }>("DELETE", `/api/import/chats/${enc(job)}`),
  importCommit: (source: string, items: ImportItem[]) =>
    req<{ memory_ids: string[] }>("POST", "/api/import/commit", { source, items }),
  sortPlan: (topic?: string | null) => req<SortRow[]>("GET", `/api/sort${topic ? `?topic=${enc(topic)}` : ""}`),
  sortMove: (moves: { memory_id: string; topic: string }[]) => req<{ moved: number }>("POST", "/api/sort", { moves }),
  async exportText(format: string, includePrivate: boolean): Promise<string> {
    const res = await fetch(`/api/export?format=${format}&include_private=${includePrivate}`, { headers: HEADERS });
    if (!res.ok) throw new Error(res.statusText);
    return res.text();
  },
};
