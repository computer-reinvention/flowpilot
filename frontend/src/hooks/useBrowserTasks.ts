import { useEffect, useRef, useState } from "react";
import { getToken } from "@/lib/api";
import { BrowserTaskClient, TASKS, TaskHttpError, pauseKey, terminal, viewerPath } from "@/lib/browserTasks";
import type { Grant, Profile, Start, TaskView } from "@/lib/browserTasks";

export function useBrowserTasks() {
  const [client, setClient] = useState<BrowserTaskClient | null>(null);
  const [task, setTask] = useState<TaskView | null>(null);
  const [taskId, setTaskId] = useState("");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Start | null>(null);
  const [grant, setGrant] = useState<(Grant & { pause: string }) | null>(null);
  const selected = useRef("");
  const current = useRef<TaskView | null>(null);
  const alive = useRef(false);
  const lock = useRef(false);
  const viewer = useRef<Window | null>(null);
  function closeWindow() { viewer.current?.close(); viewer.current = null; }

  useEffect(() => {
    alive.current = true;
    const instance = new BrowserTaskClient(getToken() ?? "");
    setClient(instance);
    return () => { alive.current = false; instance.dispose(); current.current = null; closeWindow(); };
  }, []);

  function accept(view: TaskView) {
    if (!alive.current || selected.current !== view.task_id) return;
    if (current.current && current.current.revision > view.revision) return;
    current.current = view;
    setTask(view);
    setGrant(old => old?.pause === pauseKey(view) ? old : null);
  }

  async function refreshProfiles() {
    if (!client) return;
    const data = await client.request<{ items: Profile[]; next_cursor?: string | null }>(`${TASKS}/profiles`);
    if (alive.current) { setProfiles(data.items); setTruncated(!!data.next_cursor); }
  }

  useEffect(() => {
    if (!client) return;
    void refreshProfiles().catch(err => { if (alive.current) setError(err.message); });
  }, [client]); // account-scoped mount

  useEffect(() => {
    if (!client || !taskId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const view = await client!.status(taskId);
        if (stopped) return;
        accept(view);
        if (terminal(view.state)) return;
      } catch (err) {
        if (stopped) return;
        setError(err instanceof Error ? err.message : "Status unavailable; reattach to the same task.");
      }
      if (!stopped) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [client, taskId]);

  useEffect(() => {
    if (!grant) { closeWindow(); return; }
    const timer = setTimeout(() => setGrant(null), Math.max(0, Date.parse(grant.expires_at) - Date.now()));
    return () => { clearTimeout(timer); closeWindow(); };
  }, [grant]);

  async function action(work: (api: BrowserTaskClient) => Promise<void>) {
    if (!client || lock.current) return;
    lock.current = true; setBusy(true); setError("");
    try { await work(client); }
    catch (err) { if (alive.current) setError(err instanceof Error ? err.message : "Request failed"); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }

  function attach(id: string) {
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(id)) { setError("Enter a valid owned task ID"); return; }
    selected.current = id; current.current = null; setTask(null); setGrant(null); setError(""); setTaskId(id);
    if (client) void action(async api => accept(await api.status(id)));
  }

  async function start(body: Start) {
    await action(async api => {
      setPending(body);
      let admission;
      try { admission = await api.start(body); }
      catch (err) {
        if (err instanceof TaskHttpError && err.status >= 400 && err.status < 500 && alive.current) setPending(null);
        throw err;
      }
      if (!alive.current) return;
      setPending(null);
      selected.current = admission.task_id; current.current = null; setTask(null); setGrant(null); setTaskId(admission.task_id);
      accept(await api.status(admission.task_id));
      await refreshProfiles();
    });
  }

  return {
    task, taskId, profiles, truncated, error, busy, ready: !!client, pending,
    grant: grant?.pause === pauseKey(task) ? grant : null,
    attach, start, retry: () => pending && start(pending),
    refresh: () => action(async api => { if (taskId) accept(await api.status(taskId)); await refreshProfiles(); }),
    cancel: () => action(async api => {
      setGrant(null);
      if (task) accept(await api.cancel(task.task_id));
    }),
    respond: (snapshot: TaskView, values: Record<string, unknown>) => action(async api => {
      if (pauseKey(snapshot) !== pauseKey(current.current)) throw new Error("Pause changed; review the current question");
      setGrant(null);
      try { accept(await api.respond(snapshot, { ...values, idempotency_key: crypto.randomUUID() })); }
      finally { if (selected.current === snapshot.task_id) accept(await api.status(snapshot.task_id)); }
    }),
    help: () => action(async api => {
      const snapshot = current.current;
      if (!snapshot) return;
      const result = await api.assistance(snapshot);
      viewerPath(result);
      if (alive.current && pauseKey(current.current) === pauseKey(snapshot) && result.interaction_id === snapshot.interaction?.interaction_id && result.generation === snapshot.interaction?.generation) {
        setGrant({ ...result, pause: pauseKey(snapshot) });
      }
    }),
    closeHelp: () => setGrant(null),
    openHelp: () => {
      if (!grant || grant.pause !== pauseKey(current.current) || Date.parse(grant.expires_at) <= Date.now()) return;
      closeWindow();
      const popup = window.open("about:blank", "_blank", "popup,width=1100,height=850");
      if (!popup) { setError("Allow popups, then open the private viewer again."); return; }
      popup.opener = null;
      const policy = popup.document.createElement("meta"); policy.name = "referrer"; policy.content = "no-referrer";
      popup.document.head.append(policy);
      popup.location.replace(viewerPath(grant));
      viewer.current = popup;
    },
    createProfile: (label: string, idempotency_key: string) => action(async api => {
      await api.request(`${TASKS}/profiles`, "POST", { label, idempotency_key });
      await refreshProfiles();
    }),
    deleteProfile: (id: string) => action(async api => {
      try { await api.request(`${TASKS}/profiles/${encodeURIComponent(id)}`, "DELETE"); }
      catch (err) {
        if (err instanceof TaskHttpError && err.code === "conflict") throw new Error("Profile is busy or changed. Finish/cancel its active task, refresh profiles, then retry deletion.");
        throw err;
      }
      await refreshProfiles();
    }),
  };
}
