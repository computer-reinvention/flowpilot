import { Loader2 } from "lucide-react";
import { FlowCard } from "./FlowCard";
import type { Flow } from "@/lib/types";

interface FlowGridProps {
  flows: Flow[];
  loading: boolean;
  error: string | null;
  onSelect: (flow: Flow) => void;
}

export function FlowGrid({ flows, loading, error, onSelect }: FlowGridProps) {
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

  if (flows.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground text-sm">
        No automation flows available yet.
      </div>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {flows.map((flow) => (
        <FlowCard key={flow.id} flow={flow} onClick={() => onSelect(flow)} />
      ))}
    </div>
  );
}
