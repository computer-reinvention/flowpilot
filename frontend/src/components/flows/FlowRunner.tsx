import {
  CheckCircle2,
  XCircle,
  Loader2,
  Circle,
  ArrowLeft,
  Hand,
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
  onCancel?: () => void;
}

const STEP_STYLE: Record<string, string> = {
  failed: "text-destructive",
  completed: "text-green-600 dark:text-green-400",
  done: "text-muted-foreground",
};

function StepIcon({ status }: { status: string }) {
  switch (status) {
    case "completed":
    case "done":
      return <CheckCircle2 className="size-3.5 shrink-0 text-green-500" />;
    case "failed":
      return <XCircle className="size-3.5 shrink-0 text-destructive" />;
    case "paused_for_auth":
      return <Hand className="size-3.5 shrink-0 text-amber-500" />;
    case "running":
    case "connecting":
    case "starting":
      return <Loader2 className="size-3.5 shrink-0 animate-spin" />;
    default:
      return <Circle className="size-3.5 shrink-0 text-muted-foreground" />;
  }
}

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  starting:       { label: "Starting",       className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  connecting:     { label: "Connecting",     className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  running:        { label: "Running",        className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  paused_for_auth:{ label: "Login Required", className: "bg-amber-500/10 text-amber-600 border-amber-500/20 animate-pulse" },
  completed:      { label: "Completed",      className: "bg-green-500/10 text-green-600 border-green-500/20" },
  failed:         { label: "Failed",         className: "bg-red-500/10 text-red-600 border-red-500/20" },
  cancelled:      { label: "Cancelled",      className: "bg-gray-500/10 text-gray-600 border-gray-500/20" },
};

export function FlowRunner({
  status,
  steps,
  currentStep: _currentStep,
  error,
  sandboxId,
  onReset,
  onResume,
  onCancel,
}: FlowRunnerProps) {
  const isFinished = status === "completed" || status === "failed" || status === "cancelled";
  const isPaused = status === "paused_for_auth";
  const badgeInfo = STATUS_BADGE[status] ?? { label: status, className: "" };

  // Show the live browser canvas while the run is active; hide it once finished.
  const showBrowser = !isFinished && sandboxId;

  return (
    <div className="flex flex-col h-full">
      {/* Status strip */}
      <div className="flex items-center gap-3 px-4 py-2 border-b shrink-0">
        <Badge variant="outline" className={badgeInfo.className}>
          {badgeInfo.label}
        </Badge>
        {error && (
          <span className="text-xs text-destructive truncate">{error}</span>
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
          {!isFinished && onCancel && (
            <Button variant="ghost" size="sm" onClick={onCancel} className="h-7 text-xs gap-1">
              <X className="size-3" />
              Cancel
            </Button>
          )}
          {isFinished && (
            <Button variant="outline" size="sm" onClick={onReset} className="h-7 text-xs gap-1">
              <ArrowLeft className="size-3" />
              Back
            </Button>
          )}
        </div>
      </div>

      {/* HITL banner */}
      {isPaused && (
        <div className="bg-amber-500/10 border-b border-amber-500/30 px-4 py-2 flex items-center gap-2 shrink-0 animate-pulse">
          <Hand className="size-4 text-amber-600" />
          <span className="text-sm font-medium text-amber-700 dark:text-amber-400 text-pretty">
            The agent needs you to log in — click the browser below, then press Resume Agent
          </span>
        </div>
      )}

      {/* Main content: browser + step log */}
      <ResizablePanelGroup orientation="vertical" className="flex-1">
        <ResizablePanel defaultSize={65} minSize={20}>
          {showBrowser ? (
            <BrowserCanvas
              sandboxId={sandboxId}
              isPaused={isPaused}
              className="h-full w-full"
            />
          ) : (
            <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
              {isFinished ? "Browser session ended" : "Waiting for browser..."}
            </div>
          )}
        </ResizablePanel>

        <ResizableHandle withHandle />

        <ResizablePanel defaultSize={35} minSize={10}>
          <ScrollArea className="h-full bg-background">
            <div className="p-4 space-y-1.5">
              {steps.length === 0 && (
                <p className="text-sm text-muted-foreground text-pretty">Waiting for steps...</p>
              )}
              {steps.map((step, i) => (
                <div key={i} className="flex items-start gap-2 text-sm py-0.5">
                  <div className="mt-0.5">
                    <StepIcon status={step.status} />
                  </div>
                  <span className={cn(STEP_STYLE[step.status])}>
                    {step.message}
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
