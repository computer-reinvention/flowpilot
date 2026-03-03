import {
  CheckCircle2,
  XCircle,
  Loader2,
  Circle,
  ArrowLeft,
  Hand,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from "@/components/ui/resizable";
import type { RunStep } from "@/lib/types";

interface FlowRunnerProps {
  status: string;
  steps: RunStep[];
  currentStep: string | null;
  error: string | null;
  novncUrl: string | null;
  onReset: () => void;
  onResume?: () => void;
  onCancel?: () => void;
}

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
  starting: { label: "Starting", className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  connecting: { label: "Connecting", className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  running: { label: "Running", className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  paused_for_auth: { label: "Login Required", className: "bg-amber-500/10 text-amber-600 border-amber-500/20 animate-pulse" },
  completed: { label: "Completed", className: "bg-green-500/10 text-green-600 border-green-500/20" },
  failed: { label: "Failed", className: "bg-red-500/10 text-red-600 border-red-500/20" },
  cancelled: { label: "Cancelled", className: "bg-gray-500/10 text-gray-600 border-gray-500/20" },
};

export function FlowRunner({
  status,
  steps,
  currentStep: _currentStep,
  error,
  novncUrl,
  onReset,
  onResume,
  onCancel,
}: FlowRunnerProps) {
  const isFinished = status === "completed" || status === "failed" || status === "cancelled";
  const isPaused = status === "paused_for_auth";
  const badgeInfo = STATUS_BADGE[status] ?? { label: status, className: "" };

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

      {/* Pause overlay banner */}
      {isPaused && (
        <div className="bg-amber-500/10 border-b border-amber-500/30 px-4 py-2 flex items-center gap-2 shrink-0 animate-pulse">
          <Hand className="size-4 text-amber-600" />
          <span className="text-sm font-medium text-amber-700 dark:text-amber-400">
            The agent needs you to log in using the browser below
          </span>
        </div>
      )}

      {/* Main content: VNC + Steps */}
      <ResizablePanelGroup orientation="vertical" className="flex-1">
        <ResizablePanel defaultSize={65} minSize={20}>
          <div className="h-full w-full bg-muted/30">
            {novncUrl ? (
              <iframe
                src={novncUrl}
                className={`w-full h-full border-0 ${isPaused ? "ring-2 ring-amber-400 ring-inset" : ""}`}
                allow="fullscreen; clipboard-read; clipboard-write"
                title="Live browser view"
              />
            ) : (
              <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
                {isFinished ? "Browser session ended" : "Waiting for browser..."}
              </div>
            )}
          </div>
        </ResizablePanel>

        <ResizableHandle withHandle />

        <ResizablePanel defaultSize={35} minSize={10}>
          <ScrollArea className="h-full">
            <div className="p-4 space-y-1.5">
              {steps.length === 0 && (
                <p className="text-sm text-muted-foreground">Waiting for steps...</p>
              )}
              {steps.map((step, i) => (
                <div key={i} className="flex items-start gap-2 text-sm py-0.5">
                  <div className="mt-0.5">
                    <StepIcon status={step.status} />
                  </div>
                  <span
                    className={
                      step.status === "failed"
                        ? "text-destructive"
                        : step.status === "completed"
                          ? "text-green-600 dark:text-green-400"
                          : step.status === "done"
                            ? "text-muted-foreground"
                            : ""
                    }
                  >
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
