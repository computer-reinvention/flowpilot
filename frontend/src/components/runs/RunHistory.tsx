import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
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
  pending:
    "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 border-yellow-500/20",
};

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

export function RunHistory({ runs, flows, loading, error }: RunHistoryProps) {
  const flowMap = new Map(flows.map((f) => [f.id, f]));

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
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
          {runs.map((run) => {
            const flow = flowMap.get(run.flow_id);
            return (
              <div
                key={run.id}
                className="flex items-center justify-between rounded-md border px-3 py-2"
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
                <Badge
                  variant="outline"
                  className={STATUS_STYLES[run.status] ?? ""}
                >
                  {run.status}
                </Badge>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
