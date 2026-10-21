const TOKEN_KEY = "flowpilot_token";
let accountRequests = new AbortController();

function invalidateRequests() {
  accountRequests.abort();
  accountRequests = new AbortController();
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  invalidateRequests();
  localStorage.setItem(TOKEN_KEY, token);
  window.dispatchEvent(new Event("flowpilot-auth-changed"));
}

export function clearToken(): void {
  invalidateRequests();
  localStorage.removeItem(TOKEN_KEY);
  window.dispatchEvent(new Event("flowpilot-auth-changed"));
}

export class ApiError extends Error {
  status: number;
  constructor(
    status: number,
    message: string,
  ) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options.headers as Record<string, string>) ?? {}),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const accountSignal = accountRequests.signal;
  const signal = options.signal ? AbortSignal.any([accountSignal, options.signal]) : accountSignal;
  const res = await fetch(path, { ...options, headers, signal, cache: "no-store", redirect: "error" });
  signal.throwIfAborted();
  if (getToken() !== token) throw new DOMException("Account changed", "AbortError");
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const json = await res.json();
      detail = typeof json.detail === "string" ? json.detail : json.error?.code ?? detail;
    } catch {
      // ignore parse errors
    }
    signal.throwIfAborted();
    if (getToken() !== token) throw new DOMException("Account changed", "AbortError");
    if (res.status === 401) {
      clearToken();
    }
    throw new ApiError(res.status, detail);
  }
  const data = res.status === 204 ? undefined : await res.json();
  signal.throwIfAborted();
  return data as T;
}
