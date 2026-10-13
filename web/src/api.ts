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
  settings: { require_approval: boolean };
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

/** Why a pending proposal waits for the user. */
export type Hold = "approval" | "protected" | "private";

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
  /** Computed by the server: the change is still the memory's latest version. */
  can_undo: boolean;
}

export interface SortRow {
  memory_id: string;
  content: string;
  from_topic: string;
  /** Suggested topic, or null when nothing matched. */
  topic: string | null;
  confidence: number;
}

export interface Connector {
  id: string;
  name: string;
  /** cloud: a web app reaching the folder through MyMemo Cloud (cloud-chatgpt, cloud-claude, …). */
  kind: "stdio" | "http" | "cloud";
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
   * account: MyMemo Cloud, a sign-in rather than an app (see CloudStatus).
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

/** A web app using MyMemo Cloud: a connector of its own, or a tombstone once its access was removed. */
export interface CloudApp {
  id: string;
  name: string;
  revoked: boolean;
  last_seen: string | null;
  connector: Connector | null;
}

/** MyMemo Cloud on this computer (GET /api/cloud). */
export interface CloudStatus {
  linked: boolean;
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
  account: { display_name: string } | null;
  plan: CloudPlan | null;
  linked_at: string | null;
  state: "connecting" | "online" | "waiting" | "stopped";
  /**
   * offline (reconnecting) · plan (the plan doesn't include it; checked hourly) · replaced (another OpenMyMemo took
   * over) · key (link again) · unlinked · closed · folder (linked to another folder) · not_running.
   */
  reason: string | null;
  online: boolean;
  close_code: number | null;
  last_call_at: string | null;
  /** Proposals waiting in MyMemo Cloud's encrypted queue, as of the last connection. */
  queued: number | null;
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
  action: "context" | "search" | "get" | "propose" | "import";
  query?: string;
  memory_ids?: string[];
  proposal_id?: string;
  op?: string;
  result?: string;
  chars?: number;
  source?: string;
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
}

const HEADERS = { "X-OpenMyMemo": "1", "Content-Type": "application/json" };

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, { method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : res.statusText);
  }
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  state: () => req<State>("GET", "/api/state"),
  memories: (status: "active" | "archived" = "active") => req<Memory[]>("GET", `/api/memories?status=${status}`),
  addMemory: (body: { topic?: string; content: string; kind?: Kind; section?: string | null; private?: boolean }) =>
    req<Memory>("POST", "/api/memories", body),
  patchMemory: (id: string, body: Partial<Pick<Memory, "content" | "topic" | "kind" | "pinned" | "private" | "until">>) =>
    req<Memory>("PATCH", `/api/memories/${enc(id)}`, body),
  archive: (id: string) => req<Memory>("POST", `/api/memories/${enc(id)}/archive`),
  restore: (id: string) => req<Memory>("POST", `/api/memories/${enc(id)}/restore`),
  rollback: (id: string, version: number) => req<Memory>("POST", `/api/memories/${enc(id)}/rollback`, { version }),
  history: (id: string) => req<HistoryRow[]>("GET", `/api/memories/${enc(id)}/history`),
  addTopic: (body: { slug: string; title: string; description?: string; group?: string; private?: boolean }) =>
    req<Topic>("POST", "/api/topics", body),
  patchTopic: (slug: string, body: { title?: string; description?: string; private?: boolean }) =>
    req<Topic>("PATCH", `/api/topics/${enc(slug)}`, body),
  patchSettings: (body: { require_approval?: boolean }) => req<{ require_approval: boolean }>("PATCH", "/api/settings", body),
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
  context: (connector: string | null, maxChars: number) =>
    req<ContextResult>("GET", `/api/context?max_chars=${maxChars}${connector ? `&connector=${enc(connector)}` : ""}`),
  activity: (connector?: string) =>
    req<Activity[]>("GET", `/api/activity?limit=300${connector ? `&connector=${enc(connector)}` : ""}`),
  importPreview: (source: string, text: string) => req<ImportItem[]>("POST", "/api/import/preview", { source, text }),
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
