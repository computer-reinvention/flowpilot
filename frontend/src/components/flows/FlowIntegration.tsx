import { useState } from "react";
import { Plus, Trash2, Copy, Check, Link, Clock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import type { Flow, TriggerHook } from "@/lib/types";

interface FlowIntegrationProps {
  flow: Flow;
  hooks: TriggerHook[];
  hooksLoading: boolean;
  onCreateHook: (name: string) => Promise<TriggerHook>;
  onDeleteHook: (hookId: string) => Promise<void>;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  function handleCopy() {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <Button size="icon" variant="ghost" className="h-7 w-7 shrink-0" onClick={handleCopy}>
      {copied ? <Check className="size-3.5 text-green-500" /> : <Copy className="size-3.5" />}
    </Button>
  );
}

function CodeBlock({ label, code }: { label: string; code: string }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        <CopyButton text={code} />
      </div>
      <pre className="rounded-md border bg-muted/50 px-3 py-2.5 text-xs font-mono overflow-x-auto leading-relaxed whitespace-pre-wrap break-all">
        {code}
      </pre>
    </div>
  );
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function FlowIntegration({ flow, hooks, hooksLoading, onCreateHook, onDeleteHook }: FlowIntegrationProps) {
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [revealedHook, setRevealedHook] = useState<TriggerHook | null>(null);

  const baseUrl = typeof window !== "undefined" ? window.location.origin : "";

  const curlSnippet = `curl -X POST ${baseUrl}/api/v1/runs \\
  -H "Authorization: Bearer fp_live_YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"flow_id": "${flow.id}", "inputs": {}}'`;

  const jsSnippet = `const res = await fetch("${baseUrl}/api/v1/runs", {
  method: "POST",
  headers: {
    "Authorization": "Bearer fp_live_YOUR_API_KEY",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    flow_id: "${flow.id}",
    inputs: {},
  }),
});
const run = await res.json();`;

  const pySnippet = `import requests

resp = requests.post(
    "${baseUrl}/api/v1/runs",
    headers={"Authorization": "Bearer fp_live_YOUR_API_KEY"},
    json={"flow_id": "${flow.id}", "inputs": {}},
)
run = resp.json()`;

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    try {
      const hook = await onCreateHook(name.trim());
      setRevealedHook(hook);
      setName("");
      setShowForm(false);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-8">
      {/* API Snippets */}
      <div className="space-y-4">
        <div>
          <h3 className="text-sm font-medium">API</h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            Start runs programmatically with your API key.
          </p>
        </div>
        <div className="space-y-4">
          <CodeBlock label="curl" code={curlSnippet} />
          <CodeBlock label="JavaScript" code={jsSnippet} />
          <CodeBlock label="Python" code={pySnippet} />
        </div>
      </div>

      {/* Trigger Hooks */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-medium">Trigger Hooks</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              Public URLs that start a run without authentication. Like deploy hooks.
            </p>
          </div>
          {!showForm && (
            <Button size="sm" variant="outline" onClick={() => setShowForm(true)} className="gap-1.5 shrink-0">
              <Plus className="size-3.5" />
              Create Hook
            </Button>
          )}
        </div>

        {showForm && (
          <form onSubmit={handleCreate} className="flex items-end gap-2 rounded-lg border bg-muted/30 px-4 py-3">
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="hook-name" className="text-xs">Hook name</Label>
              <Input
                id="hook-name"
                placeholder="e.g. GitHub Actions, Zapier"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                className="h-8 text-sm"
              />
            </div>
            <Button type="submit" size="sm" disabled={creating || !name.trim()} className="h-8">
              {creating ? "Creating..." : "Create"}
            </Button>
            <Button type="button" size="sm" variant="ghost" className="h-8" onClick={() => { setShowForm(false); setName(""); }}>
              Cancel
            </Button>
          </form>
        )}

        {revealedHook && (
          <div className="rounded-lg border border-green-500/20 bg-green-500/5 p-4 space-y-2">
            <p className="text-sm font-medium flex items-center gap-1.5">
              <Link className="size-4" />
              Hook Created — <span className="text-green-600">{revealedHook.name}</span>
            </p>
            <div className="relative">
              <pre className="rounded-md border bg-muted/50 px-3 py-2.5 text-xs font-mono break-all pr-10 leading-relaxed">
                {revealedHook.url}
              </pre>
              <div className="absolute right-1 top-1">
                <CopyButton text={revealedHook.url} />
              </div>
            </div>
            <CodeBlock
              label="Test it"
              code={`curl -X POST ${revealedHook.url} \\\n  -H "Content-Type: application/json" \\\n  -d '{"inputs": {}}'`}
            />
            <Button size="sm" variant="ghost" onClick={() => setRevealedHook(null)}>
              Dismiss
            </Button>
          </div>
        )}

        {hooksLoading ? (
          <div className="text-sm text-muted-foreground py-4 text-center">Loading hooks...</div>
        ) : hooks.length === 0 && !revealedHook ? (
          <div className="rounded-lg border border-dashed py-8 text-center">
            <Link className="size-6 mx-auto mb-2 text-muted-foreground/40" />
            <p className="text-sm text-muted-foreground">No trigger hooks yet</p>
            <p className="text-xs text-muted-foreground/60 mt-0.5">Create one to trigger this flow via URL</p>
          </div>
        ) : (
          <div className="space-y-2">
            {hooks.map((hook) => (
              <div key={hook.id} className="flex items-center gap-3 rounded-lg border bg-card px-4 py-3">
                <Link className="size-4 text-muted-foreground shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{hook.name}</p>
                  <div className="flex items-center gap-3 mt-0.5">
                    <span className="font-mono text-xs text-muted-foreground truncate max-w-[200px]">
                      ...{hook.token.slice(-8)}
                    </span>
                    <span className="text-xs text-muted-foreground flex items-center gap-1">
                      <Clock className="size-3" />
                      {formatDate(hook.created_at)}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      Last used: {hook.last_used_at ? formatDate(hook.last_used_at) : "never"}
                    </span>
                  </div>
                </div>
                <CopyButton text={hook.url} />
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button size="icon" variant="ghost" className="h-7 w-7 text-muted-foreground hover:text-destructive shrink-0">
                      <Trash2 className="size-3.5" />
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>Delete Trigger Hook</AlertDialogTitle>
                      <AlertDialogDescription>
                        Delete <span className="font-medium text-foreground">"{hook.name}"</span>? Any integrations using this hook URL will stop working.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>Cancel</AlertDialogCancel>
                      <AlertDialogAction
                        onClick={() => onDeleteHook(hook.id)}
                        className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                      >
                        Delete
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
