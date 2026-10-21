import { useEffect, useState } from "react";
import { useBrowserTasks } from "@/hooks/useBrowserTasks";
import { pauseKey, terminal } from "@/lib/browserTasks";
import type { TaskView } from "@/lib/browserTasks";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

function PendingInteraction({ task, busy, respond, help }: {
  task: TaskView; busy: boolean; respond: (task: TaskView, values: Record<string, unknown>) => Promise<void>; help: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const pause = task.interaction!;
  const expired = Date.parse(pause.expires_at) <= Date.now();
  useEffect(() => {
    const timer = setTimeout(() => setValues({}), Math.min(2147483647, Math.max(0, Date.parse(pause.expires_at) - Date.now())));
    return () => clearTimeout(timer);
  }, [pause.expires_at]);
  return <section className="space-y-3 rounded border p-4">
    <h3 className="font-semibold">{pause.message}</h3>
    <p className="text-xs">Pause {pause.interaction_id} · generation {pause.generation} · expires {new Date(pause.expires_at).toLocaleString()}</p>
    {pause.action_id && <p>Proposed action: {pause.action_id}. Declining does not authorize it.</p>}
    {pause.kind === "input" && <form className="space-y-3" autoComplete="off" onSubmit={e => {
      e.preventDefault(); const submitted = Object.fromEntries((pause.fields ?? []).map(field => [field.name, values[field.name] ?? ""]));
      setValues({}); void respond(task, { values: submitted });
    }}>
      {pause.fields?.map(field => <label className="block space-y-1" key={field.name}>
        <span>{field.label}</span>
        {field.kind === "select" ? <select className="block border p-2" disabled={busy || expired} required value={values[field.name] ?? ""} onChange={e => setValues(v => ({ ...v, [field.name]: e.target.value }))}>
          <option value="">Choose…</option>{field.options?.map(option => <option key={option}>{option}</option>)}
        </select> : <Input disabled={busy || expired} type={field.kind === "secret" ? "password" : "text"} autoComplete="off" value={values[field.name] ?? ""} onChange={e => setValues(v => ({ ...v, [field.name]: e.target.value }))} />}
      </label>)}
      <Button disabled={busy || expired}>Send answer privately</Button>
      <p className="text-xs">Values are cleared on submission, even if it fails. Refresh status before entering them again.</p>
    </form>}
    {pause.kind === "confirmation" && <div className="flex gap-2">
      <Button disabled={busy || expired} onClick={() => void respond(task, { confirmed: true })}>Approve this action</Button>
      <Button variant="outline" disabled={busy || expired} onClick={() => void respond(task, { confirmed: false })}>Decline</Button>
    </div>}
    {pause.kind === "browser" && <Button disabled={busy || expired} onClick={() => void respond(task, { browser_resolved: true })}>I resolved this browser pause — resume</Button>}
    {pause.kind === "browser" && <Button variant="outline" disabled={busy || expired} onClick={help}>Prepare private browser assistance</Button>}
    {expired && <p role="alert">This pause expired. Refresh status; do not reuse its decision.</p>}
  </section>;
}

export function BrowserTasks({ onKeys }: { onKeys: () => void }) {
  const api = useBrowserTasks();
  const [instruction, setInstruction] = useState("");
  const [mode, setMode] = useState("fresh");
  const [profile, setProfile] = useState("");
  const [reattach, setReattach] = useState("");
  const [label, setLabel] = useState("");
  const [profileKey, setProfileKey] = useState(() => crypto.randomUUID());
  const task = api.task;
  const active = !!task && !terminal(task.state);
  return <div className="mx-auto max-w-3xl space-y-6 p-6">
    <section className="space-y-2">
      <h1 className="text-xl font-semibold">Browser tasks</h1>
      <p>1. Sign in. 2. <button className="underline" onClick={onKeys}>Create an API key</button> for an HTTP client. 3. Start an isolated task below, or use the examples in <code>docs/browser-task-api.md</code>. This dashboard uses your account session, not an API key.</p>
      <p className="text-sm text-muted-foreground">Keys from one account have the same authority. They do not create separate tenants or profiles. Use separate accounts for separate people/security principals.</p>
    </section>
    {api.error && <p className="rounded border p-3 text-destructive" role="alert">{api.error}</p>}
    <form className="space-y-3 rounded border p-4" onSubmit={e => {
      e.preventDefault();
      void api.start({ task: instruction, idempotency_key: crypto.randomUUID(),
        ...(mode === "profile" ? { profile_id: profile } : {}),
        ...(mode === "session" && task ? { session_id: task.session_id } : {}),
      });
      setInstruction("");
    }}>
      <h2 className="font-semibold">Start a task / follow-up</h2>
      <label className="block">Task (do not include passwords or one-time codes)
        <Textarea maxLength={16000} required value={instruction} onChange={e => setInstruction(e.target.value)} disabled={!!api.pending} />
      </label>
      <label className="block">Browser isolation
        <select className="ml-2 border p-2" value={mode} onChange={e => setMode(e.target.value)} disabled={!!api.pending}>
          <option value="fresh">Fresh isolated profile (default)</option>
          <option value="profile">Explicitly reuse an owned profile</option>
          <option value="session" disabled={!task || !terminal(task.state)}>Follow up in the displayed session</option>
        </select>
      </label>
      {mode === "profile" && <label className="block">Owned profile
        <select className="ml-2 border p-2" required value={profile} onChange={e => setProfile(e.target.value)}>
          <option value="">Select a profile</option>
          {api.profiles.map(p => <option key={p.profile_id} value={p.profile_id} disabled={p.state !== "available"}>{p.label} · {p.profile_id} · {p.state}</option>)}
        </select>
      </label>}
      {mode === "session" && <p>Explicit session reuse: {task?.session_id} / profile {task?.profile_id}</p>}
      <Button disabled={!api.ready || api.busy || !!api.pending || (mode === "session" && (!task || active)) || (mode === "profile" && !api.profiles.some(p => p.profile_id === profile && p.state === "available"))}>Start task</Button>
      {api.pending && <div role="alert" className="space-y-2">
        <p>Admission may have succeeded. Do not submit a new task. Retry the identical request/key: <code>{api.pending.idempotency_key}</code>. This recovery request is memory-only; keep the task ID after admission.</p>
        <Button type="button" disabled={api.busy} onClick={() => void api.retry()}>Retry same admission</Button>
      </div>}
    </form>
    <form className="flex items-end gap-2" onSubmit={e => { e.preventDefault(); api.attach(reattach.trim()); setMode("fresh"); }}>
      <label className="flex-1">Reattach after refresh using an owned task ID<Input value={reattach} onChange={e => setReattach(e.target.value)} required pattern="[A-Za-z0-9_-]{1,128}" /></label>
      <Button variant="outline" disabled={api.busy}>Reattach</Button>
    </form>
    {api.taskId && !task && <p>Loading {api.taskId}… <Button variant="outline" onClick={() => void api.refresh()}>Refresh</Button></p>}
    {task && <section className="space-y-3 rounded border p-4">
      <h2 className="font-semibold">{task.state} · revision {task.revision}</h2>
      <dl className="break-all text-sm"><dt>Task — save this ID to reattach</dt><dd>{task.task_id}</dd><dt>Session</dt><dd>{task.session_id}</dd><dt>Profile</dt><dd>{task.profile_id}</dd></dl>
      <p>{task.progress}</p>
      {task.result && <><p className="whitespace-pre-wrap">{task.result.answer}</p><ul>{task.result.source_urls.map(url => /^https?:\/\//.test(url) && <li key={url}><a href={url} rel="noreferrer noopener" target="_blank" className="underline">{url}</a></li>)}</ul></>}
      {task.error && <p role="alert">{task.error.code}: {task.error.message}</p>}
      {task.state === "interrupted" && <p>Website effects may be partial or unknown. Inspect current website state before requesting more actions. A crash cannot resume a suspended execution stack.</p>}
      {task.interaction && <PendingInteraction key={pauseKey(task)} task={task} busy={api.busy} respond={api.respond} help={() => void api.help()} />}
      {api.grant && <section className="space-y-2">
        <p>Private bearer viewer; expires {new Date(api.grant.expires_at).toLocaleString()}. Never share or record its address.</p>
        <Button variant="outline" onClick={api.closeHelp}>Close private viewer</Button>
        <Button onClick={api.openHelp}>Open private viewer window</Button>
      </section>}
      <div className="flex gap-2"><Button variant="outline" disabled={api.busy} onClick={() => void api.refresh()}>Refresh status / profiles</Button>
        {active && <Button variant="destructive" disabled={api.busy || task.state === "cancelling"} onClick={() => void api.cancel()}>Cancel task</Button>}
      </div>
      {task.state === "cancelling" && <p>Cancellation requested; waiting for executor cleanup. This is not yet a terminal outcome.</p>}
    </section>}
    <section className="space-y-3 rounded border p-4">
      <h2 className="font-semibold">Owned browser-task profiles</h2>
      <p className="text-sm">Each profile isolates website cookies/login, script memory and conversational history. Explicit reuse restores durable state, not a running Python stack after a crash. Deleting removes associated scripts/history; leased profiles cannot be deleted. These are distinct from legacy flow profiles.</p>
      <form className="flex gap-2" onSubmit={e => { e.preventDefault(); void api.createProfile(label, profileKey); }}>
        <Input aria-label="New profile label" required maxLength={2000} value={label} onChange={e => { setLabel(e.target.value); setProfileKey(crypto.randomUUID()); }} />
        <Button disabled={api.busy || !api.ready}>Create isolated profile</Button>
      </form>
      {api.truncated && <p>Additional profiles exist. Pagination is not defined in protocol v1; use a known profile ID through an HTTP client.</p>}
      <ul className="space-y-2">{api.profiles.map(p => <li className="flex items-center justify-between gap-2" key={p.profile_id}>
        <span>{p.label} · <code>{p.profile_id}</code> · {p.state}</span>
        <Button variant="outline" disabled={api.busy || p.state === "leased"} onClick={() => {
          if (window.confirm(`Delete profile ${p.profile_id} and its scripts/history?`)) void api.deleteProfile(p.profile_id);
        }}>Delete</Button>
      </li>)}</ul>
    </section>
  </div>;
}
