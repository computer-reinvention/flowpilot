import {
  CheckCircle2,
  XCircle,
  Loader2,
  Circle,
  ArrowLeft,
  Hand,
  Pause,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from "@/components/ui/resizable";
import { BrowserCanvas } from "@/components/BrowserCanvas";
import type { RunStep } from "@/lib/types";

interface FlowRunnerProps {
  status: string;
  steps: RunStep[];
  currentStep: string | null;
  error: string | null;
  sandboxId: string | null;
  onReset: () => void;
  onResume?: () => void;
  onPause?: () => void;
  onCancel?: () => void;
}

const STEP_STYLE: Record<string, string> = {
  failed: "text-destructive",
  completed: "text-green-600 dark:text-green-400",
  done: "text-muted-foreground",
  paused_for_hitl: "text-amber-600 dark:text-amber-400",
};

function StepIcon({ status }: { status: string }) {
  switch (status) {
    case "completed":
    case "done":
      return <CheckCircle2 className="size-3 shrink-0 text-green-500" />;
    case "failed":
      return <XCircle className="size-3 shrink-0 text-destructive" />;
    case "paused_for_hitl":
      return <Hand className="size-3 shrink-0 text-amber-500" />;
    case "running":
    case "connecting":
    case "starting":
      return <Loader2 className="size-3 shrink-0 animate-spin" />;
    default:
      return <Circle className="size-3 shrink-0 text-muted-foreground/50" />;
  }
}

function stepTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Parse "[category] reason" format from HITL messages. */
function parseHitlMessage(msg: string): { category: string; reason: string } | null {
  if (!msg.startsWith("[")) return null;
  const end = msg.indexOf("]");
  if (end < 0) return null;
  return { category: msg.slice(1, end), reason: msg.slice(end + 2).trim() };
}

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  starting:        { label: "Starting",            className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  connecting:      { label: "Connecting",          className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  running:         { label: "Running",             className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  paused_for_hitl: { label: "Needs Input",         className: "bg-amber-500/10 text-amber-600 border-amber-500/20" },
  completed:       { label: "Completed",           className: "bg-green-500/10 text-green-600 border-green-500/20" },
  failed:          { label: "Failed",              className: "bg-red-500/10 text-red-600 border-red-500/20" },
  cancelled:       { label: "Cancelled",           className: "bg-gray-500/10 text-gray-600 border-gray-500/20" },
};

export function FlowRunner({
  status,
  steps,
  currentStep: _currentStep,
  error,
  sandboxId,
  onReset,
  onResume,
  onPause,
  onCancel,
}: FlowRunnerProps) {
  const isFinished = status === "completed" || status === "failed" || status === "cancelled";
  const isPaused = status === "paused_for_hitl";
  const badgeInfo = STATUS_BADGE[status] ?? { label: status, className: "" };
  const showBrowser = !isFinished && sandboxId;

  // Extract HITL category/reason from the most recent paused step
  const hitlStep = isPaused ? [...steps].reverse().find((s) => s.status === "paused_for_hitl") : null;
  const hitlParsed = hitlStep ? parseHitlMessage(hitlStep.message) : null;

  return (
    <div className="flex flex-col h-full">
      {/* Status strip */}
      <div className="flex items-center gap-2 px-4 py-2 border-b shrink-0">
        <Button variant="ghost" size="icon" onClick={onReset} className="size-7 shrink-0" aria-label="Go back">
          <ArrowLeft className="size-3.5" />
        </Button>
        <Badge variant="outline" className={cn("text-xs", badgeInfo.className)}>
          {badgeInfo.label}
        </Badge>
        <span className="text-xs text-muted-foreground tabular-nums">
          {steps.length} step{steps.length !== 1 ? "s" : ""}
        </span>
        {error && (
          <span className="text-xs text-destructive truncate ml-1">{error}</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {isPaused && onResume && (
            <Button
              size="sm"
              onClick={onResume}
              className="bg-amber-600 hover:bg-amber-700 text-white h-7 text-xs"
            >
              Resume Agent
            </Button>
          )}
          {status === "running" && onPause && (
            <Button variant="ghost" size="sm" onClick={onPause} className="h-7 text-xs gap-1">
              <Pause className="size-3" />
              Pause
            </Button>
          )}
          {!isFinished && onCancel && (
            <Button variant="ghost" size="sm" onClick={onCancel} className="h-7 text-xs gap-1">
              <X className="size-3" />
              Cancel
            </Button>
          )}
        </div>
      </div>

      {/* HITL banner — no animation per baseline-ui */}
      {isPaused && (
        <div className="bg-amber-500/10 border-b border-amber-500/20 px-4 py-2 flex items-start gap-2 shrink-0">
          <Hand className="size-4 text-amber-600 shrink-0 mt-px" />
          <div className="min-w-0">
            <span className="text-sm font-medium text-amber-700 dark:text-amber-400">
              {hitlParsed ? (
                <>
                  <span className="font-mono text-xs border border-amber-500/30 rounded px-1 py-px mr-1.5 bg-amber-500/10 uppercase tracking-wide">
                    {hitlParsed.category}
                  </span>
                  {hitlParsed.reason || "Human assistance required"}
                </>
              ) : (
                "The agent needs your help — interact with the browser below, then press Resume Agent"
              )}
            </span>
          </div>
        </div>
      )}

      {/* Main content */}
      <ResizablePanelGroup orientation="vertical" className="flex-1">
        <ResizablePanel defaultSize={65} minSize={20}>
          {showBrowser ? (
            <BrowserCanvas sandboxId={sandboxId} isPaused={isPaused} className="h-full w-full" />
          ) : (
            <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
              {isFinished ? "Browser session ended" : "Waiting for browser…"}
            </div>
          )}
        </ResizablePanel>

        <ResizableHandle withHandle />

        <ResizablePanel defaultSize={35} minSize={10}>
          <ScrollArea className="h-full bg-background">
            <div className="p-3 space-y-px font-mono">
              {steps.length === 0 && (
                <p className="text-xs text-muted-foreground px-1 py-1">Waiting for steps…</p>
              )}
              {steps.map((step, i) => (
                <div key={i} className="flex items-start gap-2 py-0.5 px-1 rounded hover:bg-muted/50">
                  <span className="mt-[3px] shrink-0">
                    <StepIcon status={step.status} />
                  </span>
                  <span className={cn("text-xs flex-1 min-w-0 break-words", STEP_STYLE[step.status])}>
                    {step.message}
                  </span>
                  <span className="text-[10px] text-muted-foreground/50 shrink-0 tabular-nums mt-px">
                    {stepTime(step.timestamp)}
                  </span>
                </div>
              ))}
            </div>
          </ScrollArea>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  );
}
