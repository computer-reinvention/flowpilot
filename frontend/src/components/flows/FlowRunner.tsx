import { useState } from "react";
import {
  CheckCircle2,
  XCircle,
  Loader2,
  Circle,
  Eye,
  EyeOff,
  ArrowLeft,
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
}

function StepIcon({ status }: { status: string }) {
  switch (status) {
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-green-500" />;
    case "failed":
      return <XCircle className="h-4 w-4 text-destructive" />;
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
  currentStep,
  error,
  novncUrl,
  onReset,
}: FlowRunnerProps) {
  const [showVnc, setShowVnc] = useState(false);
  const isFinished = status === "completed" || status === "failed";

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg">
            {isFinished
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
                        : ""
                  }
                >
                  {step.message}
                </span>
              </div>
            ))}
            {currentStep && !isFinished && (
              <div className="flex items-center gap-2 text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>{currentStep}</span>
              </div>
            )}
          </div>
        </ScrollArea>

        {error && (
          <p className="text-sm text-destructive">{error}</p>
        )}

        {novncUrl && (
          <>
            <Separator />
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
            {showVnc && (
              <div className="rounded-lg border overflow-hidden">
                <iframe
                  src={novncUrl}
                  className="w-full h-80 border-0 bg-muted"
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
