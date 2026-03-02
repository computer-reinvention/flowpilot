import { useState } from "react";
import {
  CheckCircle2,
  XCircle,
  Loader2,
  Circle,
  Eye,
  EyeOff,
  ArrowLeft,
  Hand,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import type { RunStep } from "@/lib/types";

interface FlowRunnerProps {
  status: string;
  steps: RunStep[];
  currentStep: string | null;
  error: string | null;
  novncUrl: string | null;
  onReset: () => void;
  onResume?: () => void;
}

function StepIcon({ status }: { status: string }) {
  switch (status) {
    case "completed":
    case "done":
      return <CheckCircle2 className="h-4 w-4 text-green-500" />;
    case "failed":
      return <XCircle className="h-4 w-4 text-destructive" />;
    case "paused_for_auth":
      return <Hand className="h-4 w-4 text-amber-500" />;
    case "running":
    case "connecting":
    case "starting":
      return <Loader2 className="h-4 w-4 animate-spin" />;
    default:
      return <Circle className="h-4 w-4 text-muted-foreground" />;
  }
}

export function FlowRunner({
  status,
  steps,
  currentStep: _currentStep,
  error,
  novncUrl,
  onReset,
  onResume,
}: FlowRunnerProps) {
  const [showVnc, setShowVnc] = useState(false);
  const isFinished = status === "completed" || status === "failed";
  const isPaused = status === "paused_for_auth";

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg">
            {isPaused
              ? "Login Required"
              : isFinished
                ? status === "completed"
                  ? "Run Complete"
                  : "Run Failed"
                : "Running..."}
          </CardTitle>
          {isFinished && (
            <Button variant="outline" size="sm" onClick={onReset}>
              <ArrowLeft className="mr-1 h-3 w-3" />
              Back
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <ScrollArea className="h-64">
          <div className="space-y-2">
            {steps.map((step, i) => (
              <div key={i} className="flex items-center gap-2 text-sm">
                <StepIcon status={step.status} />
                <span
                  className={
                    step.status === "failed"
                      ? "text-destructive"
                      : step.status === "completed"
                        ? "text-green-500"
                        : step.status === "done"
                          ? "text-muted-foreground"
                          : ""
                  }
                >
                  {step.message}
                </span>
              </div>
            ))}
            {/* currentStep is already shown as the last step in the list */}
          </div>
        </ScrollArea>

        {error && (
          <p className="text-sm text-destructive">{error}</p>
        )}

        {isPaused && (
          <div className="rounded-lg border border-amber-400 bg-amber-50 dark:bg-amber-950/30 p-4 space-y-3">
            <div className="flex items-center gap-2 text-amber-700 dark:text-amber-400">
              <Hand className="h-4 w-4" />
              <span className="text-sm font-medium">
                The agent needs you to log in using the browser below
              </span>
            </div>
            {onResume && (
              <Button
                onClick={onResume}
                className="w-full bg-amber-600 hover:bg-amber-700 text-white"
              >
                Resume Agent
              </Button>
            )}
          </div>
        )}

        {novncUrl && (
          <>
            <Separator />
            {!isPaused && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setShowVnc((v) => !v)}
                className="gap-1"
              >
                {showVnc ? (
                  <EyeOff className="h-3 w-3" />
                ) : (
                  <Eye className="h-3 w-3" />
                )}
                {showVnc ? "Hide" : "Watch"} live browser
              </Button>
            )}
            {(showVnc || isPaused) && (
              <div className={`rounded-lg border overflow-hidden ${isPaused ? "border-amber-400 border-2" : ""}`}>
                <iframe
                  src={novncUrl}
                  className="w-full h-80 border-0 bg-muted"
                  allow="fullscreen; clipboard-read; clipboard-write"
                  title="Live browser view"
                />
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
