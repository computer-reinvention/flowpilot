import { useState } from "react";
import {
  Sparkles,
  Globe,
  Zap,
  Play,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

interface OnboardingProps {
  onCreate: () => void;
}

const STEPS = [
  {
    icon: Globe,
    color: "text-blue-500",
    bg: "bg-blue-500/10",
    title: "Create a flow",
    body: "Give it a name, a target URL, and describe the task in plain English. That's it — no code, no selectors, no scripts.",
  },
  {
    icon: Sparkles,
    color: "text-violet-500",
    bg: "bg-violet-500/10",
    title: "The agent explores",
    body: "An AI agent visits the site, maps the journey, discovers what inputs are needed, and saves reusable scripts for every repeatable step.",
  },
  {
    icon: Zap,
    color: "text-yellow-500",
    bg: "bg-yellow-500/10",
    title: "Reflexes kick in",
    body: "On future runs, cached scripts execute instantly — no LLM calls. The agent only steps in for dynamic or new interactions.",
  },
  {
    icon: Play,
    color: "text-green-500",
    bg: "bg-green-500/10",
    title: "Run it",
    body: "Trigger runs from the UI, via API, or with webhook hooks. If auth is needed, the agent pauses and you take over the browser.",
  },
];

export function Onboarding({ onCreate }: OnboardingProps) {
  const [step, setStep] = useState(0);
  const current = STEPS[step]!;
  const Icon = current.icon;

  return (
    <div className="max-w-md mx-auto py-12 px-4">
      {/* Card */}
      <div className="rounded-xl border bg-card p-6 space-y-5">
        {/* Icon */}
        <div className={cn("size-10 rounded-lg flex items-center justify-center", current.bg)}>
          <Icon className={cn("size-5", current.color)} />
        </div>

        {/* Content */}
        <div className="space-y-2 min-h-[72px]">
          <h2 className="text-base font-semibold">{current.title}</h2>
          <p className="text-sm text-muted-foreground leading-relaxed">
            {current.body}
          </p>
        </div>

        {/* Dots + nav */}
        <div className="flex items-center justify-between pt-2">
          <div className="flex items-center gap-1.5">
            {STEPS.map((_, i) => (
              <button
                key={i}
                onClick={() => setStep(i)}
                className={cn(
                  "size-1.5 rounded-full transition-all",
                  i === step ? "bg-foreground w-4" : "bg-muted-foreground/30 hover:bg-muted-foreground/50",
                )}
              />
            ))}
          </div>

          <div className="flex items-center gap-2">
            {step > 0 && (
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                onClick={() => setStep(step - 1)}
              >
                <ChevronLeft className="size-4" />
              </Button>
            )}
            {step < STEPS.length - 1 ? (
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                onClick={() => setStep(step + 1)}
              >
                <ChevronRight className="size-4" />
              </Button>
            ) : (
              <Button size="sm" onClick={onCreate} className="gap-1.5">
                Create your first flow
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
