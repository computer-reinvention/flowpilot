import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import type { Run, Flow } from "@/lib/types";
import { ACTIVE_RUN_STATUSES } from "@/lib/types";

interface RunHistoryProps {
  runs: Run[];
  flows: Flow[];
  loading: boolean;
  error: string | null;
  onSelect: (run: Run) => void;
}

const STATUS_LABEL: Record<string, string> = {
  pending:         "Pending",
  connecting:      "Connecting",
  running:         "Running",
  paused_for_hitl: "Needs Input",
  completed:       "Completed",
  failed:          "Failed",
  cancelled:       "Cancelled",
};

const STATUS_CLASS: Record<string, string> = {
  completed:       "bg-green-500/10 text-green-600 border-green-500/20",
  failed:          "bg-red-500/10 text-red-600 border-red-500/20",
  running:         "bg-blue-500/10 text-blue-600 border-blue-500/20",
  connecting:      "bg-blue-500/10 text-blue-600 border-blue-500/20",
  paused_for_hitl: "bg-amber-500/10 text-amber-600 border-amber-500/20",
  pending:         "bg-yellow-500/10 text-yellow-600 border-yellow-500/20",
  cancelled:       "bg-gray-500/10 text-gray-600 border-gray-500/20",
};

const ACTIVE_STATUSES = new Set<string>(ACTIVE_RUN_STATUSES);

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function duration(start: string, end?: string): string {
  if (!end) return "";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return "<1s";
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

export function RunHistory({ runs, flows, loading, error, onSelect }: RunHistoryProps) {
  const flowMap = new Map(flows.map((f) => [f.id, f]));

  const sorted = [...runs].sort((a, b) => {
    const aActive = ACTIVE_STATUSES.has(a.status) ? 0 : 1;
    const bActive = ACTIVE_STATUSES.has(b.status) ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
  });

  if (loading) {
    return (
      <div className="border rounded-lg overflow-hidden divide-y">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="flex items-center gap-3 px-4 py-2.5">
            <Skeleton className="size-5 rounded shrink-0" />
            <div className="flex-1 space-y-1.5">
              <Skeleton className="h-3.5 w-32" />
              <Skeleton className="h-3 w-20" />
            </div>
            <Skeleton className="h-5 w-16 rounded-full" />
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return <p className="py-8 text-center text-sm text-destructive">{error}</p>;
  }

  if (runs.length === 0) {
    return (
      <div className="border rounded-lg border-dashed py-12 text-center">
        <p className="text-sm text-muted-foreground">No runs yet.</p>
        <p className="text-xs text-muted-foreground/60 mt-1">Select a flow to start one.</p>
      </div>
    );
  }

  return (
    <div>
      <p className="mb-3 text-xs text-muted-foreground tabular-nums">
        {runs.length} run{runs.length !== 1 ? "s" : ""}
        {" · "}
        {sorted.filter((r) => ACTIVE_STATUSES.has(r.status)).length} active
      </p>
      <div className="border rounded-lg overflow-hidden divide-y">
        {sorted.map((run) => {
          const flow = flowMap.get(run.flow_id);
          const isActive = ACTIVE_STATUSES.has(run.status);
          const dur = duration(run.created_at, run.completed_at);

          return (
            <button
              key={run.id}
              className={cn(
                "w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-accent transition-colors",
                isActive && "border-l-2 border-l-blue-500",
                run.status === "paused_for_hitl" && "border-l-2 border-l-amber-500",
              )}
              onClick={() => onSelect(run)}
            >
              {/* Active indicator or flow icon */}
              <span className="size-5 shrink-0 flex items-center justify-center">
                {isActive ? (
                  <span className="relative flex size-2">
                    <span className="absolute inline-flex size-full animate-ping rounded-full bg-green-400 opacity-75" />
                    <span className="relative inline-flex size-2 rounded-full bg-green-500" />
                  </span>
                ) : (
                  <span className="text-base leading-none">{flow?.icon ?? ""}</span>
                )}
              </span>

              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium truncate">
                  {flow?.name ?? run.flow_id}
                </p>
                <p className="text-xs text-muted-foreground tabular-nums">
                  <span className="font-mono opacity-60">{run.id}</span>
                  {run.profile_id && run.profile_id !== run.user_id && (
                    <> · <span className="font-mono">{run.profile_id}</span></>
                  )}
                  {" · "}{relativeTime(run.created_at)}
                  {dur && <> · {dur}</>}
                </p>
              </div>

              <Badge variant="outline" className={cn("text-xs shrink-0", STATUS_CLASS[run.status] ?? "")}>
                {STATUS_LABEL[run.status] ?? run.status}
              </Badge>
            </button>
          );
        })}
      </div>
    </div>
  );
}
