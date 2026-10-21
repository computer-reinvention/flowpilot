// Mirrors backend/task_contracts.py v1. Credentials and input values are memory-only.
export const TASKS = "/api/v1/browser-tasks";
export type Interaction = {
  interaction_id: string; generation: number; kind: "input" | "confirmation" | "browser";
  message: string; action_id?: string; expires_at: string;
  fields?: { name: string; label: string; kind: "text" | "secret" | "select"; options?: string[] }[];
};
export type TaskView = {
  task_id: string; session_id: string; profile_id: string; revision: number;
  state: string; progress?: string; interaction?: Interaction | null;
  result?: { answer: string; source_urls: string[]; metrics?: Record<string, number> } | null;
  error?: { code: string; message: string; retryable: boolean } | null;
};
export type Admission = { task_id: string; session_id: string; profile_id: string; status_url: string; revision: number };
export type Start = { task: string; inputs?: Record<string, unknown>; profile_id?: string; session_id?: string; idempotency_key: string };
export type Profile = { profile_id: string; label: string; state: "available" | "leased" | "recovery_required"; created_at: string };
export type Grant = { assistance_url: string; expires_at: string; interaction_id: string; generation: number };
export const terminal = (state: string) => ["completed", "failed", "cancelled", "interrupted"].includes(state);
export const pauseKey = (task: TaskView | null) => task?.interaction ? `${task.task_id}:${task.interaction.interaction_id}:${task.interaction.generation}` : "";
export function viewerPath(grant: Grant) {
  if (!/^\/api\/v1\/browser-tasks\/help\/[A-Za-z0-9_-]{32,128}$/.test(grant.assistance_url)) throw new Error("Invalid viewer path");
  return grant.assistance_url;
}
export class TaskHttpError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string) {
    super(`${code} (HTTP ${status}). Refresh current state before deciding again.`);
    this.status = status; this.code = code;
  }
}
// One mounted account surface owns a client. Fence even transports ignoring abort.
export class BrowserTaskClient {
  private controller = new AbortController();
  private token: string;
  private transport: typeof fetch;
  constructor(token: string, transport: typeof fetch = fetch) { this.token = token; this.transport = transport; }
  dispose() { this.controller.abort(); this.token = ""; }
  async request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    if (!path.startsWith(TASKS) || path.includes(":") || path.includes("..")) throw new Error("Invalid task path");
    this.controller.signal.throwIfAborted();
    const response = await this.transport(path, {
      method, signal: this.controller.signal, cache: "no-store", redirect: "error",
      headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    this.controller.signal.throwIfAborted();
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const code = /^[a-z_]+$/.test(data.error?.code ?? "") ? data.error.code : "request_failed";
      throw new TaskHttpError(response.status, code);
    }
    const data = response.status === 204 ? undefined : await response.json();
    this.controller.signal.throwIfAborted();
    return data as T;
  }
  start(body: Start) { return this.request<Admission>(TASKS, "POST", body); }
  status(id: string) { return this.request<TaskView>(`${TASKS}/${encodeURIComponent(id)}`); }
  cancel(id: string) { return this.request<TaskView>(`${TASKS}/${encodeURIComponent(id)}/cancel`, "POST"); }
  respond(task: TaskView, response: Record<string, unknown>) {
    const pause = task.interaction;
    if (!pause || Date.parse(pause.expires_at) <= Date.now()) throw new Error("Pause expired; refresh status");
    return this.request<TaskView>(`${TASKS}/${encodeURIComponent(task.task_id)}/interactions/${encodeURIComponent(pause.interaction_id)}/respond`, "POST", {
      ...response, generation: pause.generation, kind: pause.kind,
    });
  }
  assistance(task: TaskView) {
    const pause = task.interaction;
    if (!pause || Date.parse(pause.expires_at) <= Date.now()) throw new Error("Pause expired; refresh status");
    return this.request<Grant>(`${TASKS}/${encodeURIComponent(task.task_id)}/assistance`, "POST", { interaction_id: pause.interaction_id, generation: pause.generation });
  }
}
