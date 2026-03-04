import { Globe, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Flow } from "@/lib/types";

interface FlowCardProps {
  flow: Flow;
  onClick: () => void;
}

export function FlowCard({ flow, onClick }: FlowCardProps) {
  return (
    <button
      className={cn(
        "w-full flex items-start gap-3 px-4 py-3 text-left",
        "hover:bg-accent transition-colors border-b last:border-b-0",
      )}
      onClick={onClick}
    >
      <span className="text-base shrink-0 w-5 text-center leading-5 mt-px">
        {flow.icon ?? <Globe className="size-4 text-muted-foreground" />}
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className="text-sm font-medium truncate">{flow.name}</span>
          <code className="text-[11px] font-mono bg-muted px-1.5 py-px rounded text-muted-foreground shrink-0">
            {flow.domain}
          </code>
          {flow.inputs.length > 0 && (
            <span className="text-[11px] text-muted-foreground shrink-0 tabular-nums">
              {flow.inputs.length}&nbsp;input{flow.inputs.length !== 1 ? "s" : ""}
            </span>
          )}
        </div>
        <p className="text-xs text-muted-foreground truncate">{flow.description}</p>
      </div>
      <ChevronRight className="size-3.5 text-muted-foreground/50 shrink-0 mt-1" />
    </button>
  );
}
