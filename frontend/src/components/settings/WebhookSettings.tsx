import { useState } from "react";
import { Plus, Trash2, Copy, Check, Webhook, Send, AlertCircle, CheckCircle2 } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import type { Webhook as WebhookType } from "@/lib/types";

const ALL_EVENTS = [
  "run.started",
  "run.paused",
  "run.completed",
  "run.failed",
  "run.cancelled",
] as const;

const EVENT_COLORS: Record<string, string> = {
  "run.started":   "bg-blue-500/10 text-blue-600 border-blue-500/20",
  "run.paused":    "bg-amber-500/10 text-amber-600 border-amber-500/20",
  "run.completed": "bg-green-500/10 text-green-600 border-green-500/20",
  "run.failed":    "bg-red-500/10 text-red-600 border-red-500/20",
  "run.cancelled": "bg-gray-500/10 text-gray-600 border-gray-500/20",
};

interface WebhookSettingsProps {
  webhooks: WebhookType[];
  loading: boolean;
  onCreate: (url: string, events: string[]) => Promise<WebhookType>;
  onDelete: (id: string) => Promise<void>;
  onTest: (id: string) => Promise<{ status: string; response_status?: number; error?: string }>;
}

function RevealSecretDialog({ webhook, onClose }: { webhook: WebhookType; onClose: () => void }) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(webhook.secret ?? "");
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Webhook className="size-4" />
            Webhook Secret
          </DialogTitle>
          <DialogDescription>
            Use this to verify incoming webhook signatures. <span className="font-medium text-foreground">Won't be shown again.</span>
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="relative">
            <div className="rounded-md border bg-muted/50 px-3 py-2.5 font-mono text-xs break-all select-all pr-10 leading-relaxed">
              {webhook.secret}
            </div>
            <Button
              size="icon"
              variant="ghost"
              className="absolute right-1 top-1 h-7 w-7"
              onClick={handleCopy}
            >
              {copied ? <Check className="size-3.5 text-green-500" /> : <Copy className="size-3.5" />}
            </Button>
          </div>
          <div className="rounded-md bg-muted/50 border px-3 py-2 space-y-1">
            <p className="text-xs font-medium">Verifying signatures</p>
            <p className="text-xs text-muted-foreground">
              Check the <code className="font-mono bg-background rounded px-0.5">X-FlowPilot-Signature</code> header.
              It contains <code className="font-mono bg-background rounded px-0.5">sha256=&lt;hmac-sha256&gt;</code> of the raw request body signed with this secret.
            </p>
          </div>
        </div>
        <div className="flex justify-end">
          <Button onClick={onClose}>Done</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function WebhookSettings({ webhooks, loading, onCreate, onDelete, onTest }: WebhookSettingsProps) {
  const [showForm, setShowForm] = useState(false);
  const [url, setUrl] = useState("");
  const [selectedEvents, setSelectedEvents] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [revealWebhook, setRevealWebhook] = useState<WebhookType | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; label: string }>>({});
  const [testing, setTesting] = useState<string | null>(null);

  function toggleEvent(event: string) {
    setSelectedEvents((prev) =>
      prev.includes(event) ? prev.filter((e) => e !== event) : [...prev, event]
    );
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const wh = await onCreate(url.trim(), selectedEvents);
      setRevealWebhook(wh);
      setUrl("");
      setSelectedEvents([]);
      setShowForm(false);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to create webhook");
    } finally {
      setCreating(false);
    }
  }

  async function handleTest(id: string) {
    setTesting(id);
    try {
      const result = await onTest(id);
      const ok = result.status === "delivered";
      setTestResults((prev) => ({
        ...prev,
        [id]: {
          ok,
          label: ok ? `Delivered (${result.response_status})` : result.error ?? `Failed (${result.response_status ?? "no response"})`,
        },
      }));
      setTimeout(() => setTestResults((prev) => { const n = { ...prev }; delete n[id]; return n; }), 5000);
    } finally {
      setTesting(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Receive real-time events when runs change state. Payloads include CDP browser URLs for HITL flows.
        </p>
        {!showForm && (
          <Button size="sm" variant="outline" onClick={() => setShowForm(true)} className="gap-1.5 shrink-0">
            <Plus className="size-3.5" />
            Add Webhook
          </Button>
        )}
      </div>

      {showForm && (
        <form
          onSubmit={handleCreate}
          className="rounded-lg border bg-muted/30 px-4 py-4 space-y-3"
        >
          <div className="space-y-1.5">
            <Label htmlFor="wh-url" className="text-xs">Endpoint URL</Label>
            <Input
              id="wh-url"
              placeholder="https://your-server.com/webhooks/flowpilot"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
              className="h-8 text-sm font-mono"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">
              Subscribe to events <span className="text-muted-foreground font-normal">(leave all unchecked to receive every event)</span>
            </Label>
            <div className="flex flex-wrap gap-2">
              {ALL_EVENTS.map((event) => (
                <button
                  key={event}
                  type="button"
                  onClick={() => toggleEvent(event)}
                  className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium transition-colors ${
                    selectedEvents.includes(event)
                      ? (EVENT_COLORS[event] ?? "bg-primary/10 text-primary border-primary/20")
                      : "bg-transparent text-muted-foreground border-border hover:border-foreground/30"
                  }`}
                >
                  {event}
                </button>
              ))}
            </div>
          </div>
          {createError && (
            <p className="text-xs text-destructive">{createError}</p>
          )}
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={creating || !url.trim()} className="h-8">
              {creating ? "Adding…" : "Add Webhook"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-8"
              onClick={() => { setShowForm(false); setUrl(""); setSelectedEvents([]); setCreateError(null); }}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <div key={i} className="flex items-start gap-3 rounded-lg border px-4 py-3">
              <Skeleton className="size-4 rounded shrink-0 mt-0.5" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-3.5 w-48" />
                <div className="flex gap-1.5">
                  <Skeleton className="h-4 w-20 rounded-full" />
                  <Skeleton className="h-4 w-20 rounded-full" />
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : webhooks.length === 0 ? (
        <div className="rounded-lg border border-dashed py-10 text-center">
          <Webhook className="size-8 mx-auto mb-2 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">No webhooks configured</p>
          <p className="text-xs text-muted-foreground/60 mt-0.5">Add one to receive run lifecycle events</p>
        </div>
      ) : (
        <div className="space-y-2">
          {webhooks.map((wh) => (
            <div key={wh.id} className="rounded-lg border bg-card px-4 py-3 space-y-2">
              <div className="flex items-start gap-3">
                <Webhook className="size-4 text-muted-foreground shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-mono truncate">{wh.url}</p>
                  <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                    {wh.events.length === 0 ? (
                      <Badge variant="secondary" className="text-xs h-5">all events</Badge>
                    ) : (
                      wh.events.map((ev) => (
                        <span
                          key={ev}
                          className={`inline-flex items-center rounded border px-1.5 py-0 text-xs ${EVENT_COLORS[ev] ?? ""}`}
                        >
                          {ev}
                        </span>
                      ))
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 px-2 text-xs gap-1"
                    onClick={() => handleTest(wh.id)}
                    disabled={testing === wh.id}
                  >
                    <Send className="size-3" />
                    {testing === wh.id ? "Sending…" : "Test"}
                  </Button>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button size="icon" variant="ghost" className="h-7 w-7 text-muted-foreground hover:text-destructive">
                        <Trash2 className="size-3.5" />
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>Delete Webhook</AlertDialogTitle>
                        <AlertDialogDescription>
                          Remove <span className="font-mono text-foreground text-xs">{wh.url}</span>? No further events will be delivered to this endpoint.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction
                          onClick={() => onDelete(wh.id)}
                          className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                        >
                          Delete
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </div>
              </div>

              {(() => {
                const tr = testResults[wh.id];
                if (!tr) return null;
                return (
                  <div className={`flex items-center gap-1.5 text-xs rounded px-2 py-1 ${
                    tr.ok ? "bg-green-500/10 text-green-600" : "bg-red-500/10 text-red-600"
                  }`}>
                    {tr.ok
                      ? <CheckCircle2 className="size-3 shrink-0" />
                      : <AlertCircle className="size-3 shrink-0" />
                    }
                    {tr.label}
                  </div>
                );
              })()}
            </div>
          ))}
        </div>
      )}

      {revealWebhook && (
        <RevealSecretDialog webhook={revealWebhook} onClose={() => setRevealWebhook(null)} />
      )}
    </div>
  );
}
