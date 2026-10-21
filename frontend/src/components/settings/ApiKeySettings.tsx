import { useState } from "react";
import { Plus, Trash2, Copy, Check, Key, Clock } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
import type { ApiKey } from "@/lib/types";

interface ApiKeySettingsProps {
  apiKeys: ApiKey[];
  loading: boolean;
  error?: string | null;
  onRefresh: () => Promise<void>;
  onCreate: (name: string) => Promise<ApiKey>;
  onDelete: (id: string) => Promise<void>;
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function RevealKeyDialog({ apiKey, onClose }: { apiKey: ApiKey; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");

  async function handleCopy() {
    try { await navigator.clipboard.writeText(apiKey.key ?? ""); setCopied(true); setCopyError(""); }
    catch { setCopyError("Clipboard unavailable. Select and copy the key manually."); }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Key className="size-4" />
            API Key Created
          </DialogTitle>
          <DialogDescription>
            Copy your key now. <span className="font-medium text-foreground">It won't be shown again.</span>
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="relative">
            <div className="rounded-md border bg-muted/50 px-3 py-2.5 font-mono text-xs break-all select-all pr-10 leading-relaxed">
              {apiKey.key}
            </div>
            <Button
              size="icon"
              variant="ghost"
              className="absolute right-1 top-1 h-7 w-7"
              onClick={handleCopy}
              aria-label="Copy newly created API key"
            >
              {copied ? <Check className="size-3.5 text-green-500" /> : <Copy className="size-3.5" />}
            </Button>
          </div>
          {copyError && <p role="alert">{copyError}</p>}
          <p className="text-xs text-muted-foreground">
            Store it in an environment variable. Never commit it to source control.
          </p>
        </div>
        <div className="flex justify-end">
          <Button onClick={onClose}>Done</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function ApiKeySettings({ apiKeys, loading, error, onRefresh, onCreate, onDelete }: ApiKeySettingsProps) {
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [revealKey, setRevealKey] = useState<ApiKey | null>(null);
  const [revokeError, setRevokeError] = useState("");
  const [revoking, setRevoking] = useState<string | null>(null);

  async function revoke(id: string) {
    setRevokeError(""); setRevoking(id);
    try { await onDelete(id); if (revealKey?.id === id) setRevealKey(null); }
    catch { setRevokeError("Revocation failed. The key may still be active; refresh and retry."); }
    finally { setRevoking(null); }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const key = await onCreate(name.trim());
      setRevealKey(key);
      setName("");
      setShowForm(false);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to create key");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Account API keys</h1>
      <p className="text-sm">All keys have this account's authority, not separate tenant identities. Revocation prevents future requests; it does not implicitly cancel admitted tasks. Newly created plaintext is shown once, only here.</p>
      {(error || revokeError) && <p role="alert" className="text-destructive">{error || revokeError}</p>}
      <Button variant="outline" disabled={loading} onClick={() => void onRefresh()}>Refresh API keys</Button>
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-muted-foreground">
            Use API keys to authenticate programmatic access to FlowPilot.
          </p>
        </div>
        {!showForm && (
          <Button size="sm" variant="outline" onClick={() => setShowForm(true)} className="gap-1.5 shrink-0">
            <Plus className="size-3.5" />
            Create Key
          </Button>
        )}
      </div>

      {showForm && (
        <form
          onSubmit={handleCreate}
          className="flex items-end gap-2 rounded-lg border bg-muted/30 px-4 py-3"
        >
          <div className="flex-1 space-y-1.5">
            <Label htmlFor="key-name" className="text-xs">Key name</Label>
            <Input
              id="key-name"
              placeholder="e.g. Production, CI/CD"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              className="h-8 text-sm"
            />
          </div>
          <Button type="submit" size="sm" disabled={creating || !name.trim()} className="h-8">
            {creating ? "Creating…" : "Create"}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-8"
            onClick={() => { setShowForm(false); setName(""); setCreateError(null); }}
          >
            Cancel
          </Button>
          {createError && (
            <p className="text-xs text-destructive">{createError}</p>
          )}
        </form>
      )}

      {loading ? (
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <div key={i} className="flex items-center gap-3 rounded-lg border px-4 py-3">
              <Skeleton className="size-4 rounded shrink-0" />
              <div className="flex-1 space-y-1.5">
                <Skeleton className="h-3.5 w-32" />
                <Skeleton className="h-3 w-48" />
              </div>
              <Skeleton className="size-7 rounded" />
            </div>
          ))}
        </div>
      ) : apiKeys.length === 0 ? (
        <div className="rounded-lg border border-dashed py-10 text-center">
          <Key className="size-8 mx-auto mb-2 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">No API keys yet</p>
          <p className="text-xs text-muted-foreground/60 mt-0.5">Create one to access the FlowPilot API</p>
        </div>
      ) : (
        <div className="space-y-2">
          {apiKeys.map((key) => (
            <div
              key={key.id}
              className="flex items-center gap-3 rounded-lg border bg-card px-4 py-3"
            >
              <Key className="size-4 text-muted-foreground shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium truncate">{key.name}</p>
                <div className="flex items-center gap-3 mt-0.5">
                  <span className="font-mono text-xs text-muted-foreground">{key.key_prefix}…</span>
                  <span className="text-xs text-muted-foreground flex items-center gap-1">
                    <Clock className="size-3" />
                    Created {formatDate(key.created_at)}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    Last used: {key.last_used_at ? formatDate(key.last_used_at) : "never"}
                  </span>
                </div>
              </div>
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button aria-label={`Revoke ${key.name}`} disabled={revoking !== null} size="icon" variant="ghost" className="h-7 w-7 text-muted-foreground hover:text-destructive shrink-0">
                    <Trash2 className="size-3.5" />
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Delete API Key</AlertDialogTitle>
                    <AlertDialogDescription>
                      Delete <span className="font-medium text-foreground">"{key.name}"</span>? Any integrations using this key will stop working immediately.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction
                      onClick={() => void revoke(key.id)}
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

      {revealKey && (
        <RevealKeyDialog apiKey={revealKey} onClose={() => setRevealKey(null)} />
      )}
    </div>
  );
}
