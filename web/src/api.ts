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
  kind: "stdio" | "http";
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
