import { Loader2, Plus } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { FlowCard } from "./FlowCard";
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

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {flows.map((flow) => (
        <FlowCard key={flow.id} flow={flow} onClick={() => onSelect(flow)} />
      ))}
      <Card
        className="cursor-pointer border-dashed transition-colors hover:bg-accent"
        onClick={onCreate}
      >
        <CardContent className="flex items-center justify-center gap-2 py-10 text-muted-foreground">
          <Plus className="h-5 w-5" />
          <span className="text-sm font-medium">New Flow</span>
        </CardContent>
      </Card>
    </div>
  );
}
