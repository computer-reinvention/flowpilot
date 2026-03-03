import { Loader2, Radio } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { Run, Flow } from "@/lib/types";

interface RunHistoryProps {
  runs: Run[];
  flows: Flow[];
  loading: boolean;
  error: string | null;
  onConnect?: (run: Run) => void;
}

const STATUS_STYLES: Record<string, string> = {
  completed:
    "bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20",
  failed:
    "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20",
  running:
    "bg-blue-500/10 text-blue-600 dark:text-blue-400 border-blue-500/20",
  connecting:
    "bg-blue-500/10 text-blue-600 dark:text-blue-400 border-blue-500/20",
  paused_for_auth:
    "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20",
  pending:
    "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 border-yellow-500/20",
  cancelled:
    "bg-gray-500/10 text-gray-600 dark:text-gray-400 border-gray-500/20",
};

const ACTIVE_STATUSES = new Set(["pending", "connecting", "running", "paused_for_auth"]);

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function duration(start: string, end?: string): string {
  if (!end) return "-";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return "<1s";
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  return `${mins}m ${rem}s`;
}

export function RunHistory({ runs, flows, loading, error, onConnect }: RunHistoryProps) {
  const flowMap = new Map(flows.map((f) => [f.id, f]));

  // Sort active runs to top
  const sorted = [...runs].sort((a, b) => {
    const aActive = ACTIVE_STATUSES.has(a.status) ? 0 : 1;
    const bActive = ACTIVE_STATUSES.has(b.status) ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    return 0; // preserve original order within group
  });

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-center py-12 text-destructive text-sm">{error}</div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground text-sm">
        No runs yet. Select a flow and run it to see history here.
      </div>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Run History</CardTitle>
        <CardDescription>
          {runs.length} run{runs.length !== 1 && "s"}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          {sorted.map((run) => {
            const flow = flowMap.get(run.flow_id);
            const isActive = ACTIVE_STATUSES.has(run.status);
            return (
              <div
                key={run.id}
                className={`flex items-center justify-between rounded-md border px-3 py-2 ${
                  isActive ? "border-l-2 border-l-green-500" : ""
                }`}
              >
                <div className="flex items-center gap-3 min-w-0">
                  {flow?.icon && (
                    <span className="text-base shrink-0">{flow.icon}</span>
                  )}
                  <div className="min-w-0">
                    <p className="text-sm font-medium truncate">
                      {flow?.name ?? run.flow_id}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {formatTime(run.created_at)}
                      {run.completed_at &&
                        ` \u00B7 ${duration(run.created_at, run.completed_at)}`}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge
                    variant="outline"
                    className={STATUS_STYLES[run.status] ?? ""}
                  >
                    {run.status}
                  </Badge>
                  {isActive && onConnect && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs gap-1"
                      onClick={() => onConnect(run)}
                    >
                      <Radio className="size-3" />
                      Connect
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
