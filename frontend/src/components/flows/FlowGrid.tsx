import { Plus } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { FlowCard } from "./FlowCard";
import { Onboarding } from "./Onboarding";
import type { Flow } from "@/lib/types";

interface FlowGridProps {
  flows: Flow[];
  loading: boolean;
  error: string | null;
  onSelect: (flow: Flow) => void;
  onCreate: () => void;
}

export function FlowGrid({ flows, loading, error, onSelect, onCreate }: FlowGridProps) {
  if (loading) {
    return (
      <div className="border rounded-lg overflow-hidden divide-y">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="flex items-start gap-3 px-4 py-3">
            <Skeleton className="size-5 rounded shrink-0 mt-px" />
            <div className="flex-1 space-y-1.5">
              <div className="flex items-center gap-2">
                <Skeleton className="h-3.5 w-28" />
                <Skeleton className="h-3.5 w-16 rounded" />
              </div>
              <Skeleton className="h-3 w-2/3" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <p className="py-8 text-center text-sm text-destructive">{error}</p>
    );
  }

  if (flows.length === 0) {
    return <Onboarding onCreate={onCreate} />;
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-muted-foreground tabular-nums">
          {flows.length} flow{flows.length !== 1 ? "s" : ""}
        </p>
        <button
          onClick={onCreate}
          className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          <Plus className="size-3.5" />
          New flow
        </button>
      </div>
      <div className="border rounded-lg overflow-hidden divide-y">
        {flows.map((flow) => (
          <FlowCard key={flow.id} flow={flow} onClick={() => onSelect(flow)} />
        ))}
      </div>
    </div>
  );
}
