import { useState } from "react";
import {
  Globe,
  FormInput,
  ShieldAlert,
  MousePointerClick,
  FileSearch,
  Shuffle,
  ChevronRight,
  ChevronDown,
  Zap,
  Loader2,
  CheckCircle2,
  XCircle,
  RefreshCw,
  StopCircle,
  AlertTriangle,
  Hand,
  Check,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Flow, ExplorationStage } from "@/lib/types";

const TYPE_CONFIG: Record<
  string,
  { icon: typeof Globe; color: string; label: string }
> = {
  page: { icon: Globe, color: "text-blue-500", label: "Page" },
  form: { icon: FormInput, color: "text-violet-500", label: "Form" },
  auth_wall: { icon: ShieldAlert, color: "text-amber-500", label: "Auth wall" },
  navigation: { icon: MousePointerClick, color: "text-emerald-500", label: "Navigation" },
  extraction: { icon: FileSearch, color: "text-cyan-500", label: "Extraction" },
  dynamic: { icon: Shuffle, color: "text-orange-500", label: "Dynamic" },
};

function StageNode({
  stage,
  depth = 0,
  hitlNode,
}: {
  stage: ExplorationStage;
  depth?: number;
  /** Rendered after the last leaf node in the tree */
  hitlNode?: React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(true);
  const config = TYPE_CONFIG[stage.type] ?? TYPE_CONFIG["page"]!;
  const Icon = config.icon;
  const hasChildren = stage.children.length > 0;

  // hitlNode attaches after the deepest last child
  const lastChildIndex = stage.children.length - 1;

  return (
    <div className={cn(depth > 0 && "ml-5 border-l border-border/50 pl-0")}>
      <div
        className={cn(
          "group flex items-start gap-2 py-1.5 px-2 rounded-md",
          "hover:bg-muted/50 transition-colors cursor-default",
          depth > 0 && "ml-1",
        )}
      >
        {/* Expand/collapse toggle */}
        <button
          className={cn(
            "mt-0.5 shrink-0 size-4 flex items-center justify-center rounded",
            hasChildren ? "hover:bg-muted" : "invisible",
          )}
          onClick={() => hasChildren && setExpanded(!expanded)}
          tabIndex={hasChildren ? 0 : -1}
        >
          {hasChildren &&
            (expanded ? (
              <ChevronDown className="size-3 text-muted-foreground" />
            ) : (
              <ChevronRight className="size-3 text-muted-foreground" />
            ))}
        </button>

        {/* Type icon */}
        <Icon className={cn("size-3.5 mt-0.5 shrink-0", config.color)} />

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">{stage.title}</span>
            <span
              className={cn(
                "text-[10px] font-mono px-1 py-px rounded border",
                config.color,
                "bg-current/5 border-current/20",
              )}
              style={{ backgroundColor: "transparent" }}
            >
              {config.label}
            </span>
          </div>
          <p className="text-xs text-muted-foreground truncate mt-px">
            {stage.url}
          </p>
          {stage.note && (
            <p className="text-xs text-muted-foreground/70 mt-0.5 italic">
              {stage.note}
            </p>
          )}
          {stage.reflex_name && (
            <div className="flex items-center gap-1.5 mt-1">
              <Zap className="size-3 text-yellow-500" />
              <code className="text-xs font-mono bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 px-1.5 py-px rounded">
                {stage.reflex_name}
              </code>
            </div>
          )}
        </div>
      </div>

      {/* Children */}
      {expanded &&
        hasChildren &&
        stage.children.map((child, i) => (
          <StageNode
            key={child.id}
            stage={child}
            depth={depth + 1}
            hitlNode={i === lastChildIndex ? hitlNode : undefined}
          />
        ))}

      {/* HITL node attaches here if this is a leaf (no children) */}
      {!hasChildren && hitlNode && (
        <div className={cn("ml-5 border-l border-border/50 pl-0")}>
          <div className="ml-1">{hitlNode}</div>
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Inline HITL form components
// -----------------------------------------------------------------------

function HitlInputForm({
  fields,
  values,
  onChange,
  onSubmit,
}: {
  fields: NonNullable<Flow["exploration_input_request"]>;
  values: Record<string, string>;
  onChange: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  onSubmit: () => void;
}) {
  return (
    <div className="rounded-md border border-dashed bg-muted/40 px-3 py-3 my-1 space-y-3">
      <div className="flex items-center gap-2">
        <Hand className="size-3.5 text-muted-foreground shrink-0" />
        <span className="text-xs font-medium text-foreground">
          Enter values for these fields
        </span>
      </div>
      <div className="space-y-2">
        {fields.map((field) => (
          <div key={field.name} className="space-y-1">
            <Label htmlFor={`hitl-${field.name}`} className="text-xs text-muted-foreground">
              {field.label || field.name}
              {field.description && (
                <span className="ml-1 opacity-60">— {field.description}</span>
              )}
            </Label>
            {field.type === "select" && field.options ? (
              <Select
                value={values[field.name] ?? ""}
                onValueChange={(val) => onChange((prev) => ({ ...prev, [field.name]: val }))}
              >
                <SelectTrigger id={`hitl-${field.name}`} className="h-8 text-sm">
                  <SelectValue placeholder={field.label || field.name} />
                </SelectTrigger>
                <SelectContent>
                  {field.options.map((opt) => (
                    <SelectItem key={opt} value={opt}>{opt}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id={`hitl-${field.name}`}
                type="text"
                placeholder={field.description || field.label || field.name}
                className="h-8 text-sm"
                value={values[field.name] ?? ""}
                onChange={(e) => onChange((prev) => ({ ...prev, [field.name]: e.target.value }))}
              />
            )}
          </div>
        ))}
      </div>
      <Button size="sm" onClick={onSubmit} className="w-full">
        Submit Values
      </Button>
    </div>
  );
}

function HitlConfirmation({
  summary,
  onApprove,
  onDecline,
}: {
  summary: string;
  onApprove: () => void;
  onDecline: () => void;
}) {
  return (
    <div className="rounded-md border border-dashed bg-muted/40 px-3 py-3 my-1 space-y-2">
      <div className="flex items-center gap-2">
        <Hand className="size-3.5 text-muted-foreground shrink-0" />
        <span className="text-xs font-medium text-foreground">
          Confirm action
        </span>
      </div>
      <p className="text-sm text-foreground">{summary}</p>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" onClick={onDecline} className="flex-1 gap-1">
          <X className="size-3.5" /> Decline
        </Button>
        <Button size="sm" onClick={onApprove} className="flex-1 gap-1">
          <Check className="size-3.5" /> Approve
        </Button>
      </div>
    </div>
  );
}

function HitlAuthWall({ onResume }: { onResume: () => void }) {
  return (
    <div className="rounded-md border border-dashed bg-muted/40 px-3 py-3 my-1 space-y-2">
      <div className="flex items-center gap-2">
        <Hand className="size-3.5 text-muted-foreground shrink-0" />
        <span className="text-xs font-medium text-foreground">
          Waiting for you
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        Log in or complete the action in the browser above, then resume.
      </p>
      <Button size="sm" onClick={onResume} className="w-full">
        Resume Exploration
      </Button>
    </div>
  );
}

// -----------------------------------------------------------------------
// Main tree
// -----------------------------------------------------------------------

interface ExplorationTreeProps {
  flow: Flow;
  onExplore?: () => void;
  onCancel?: () => void;
  onResume?: (body?: Record<string, unknown>) => void;
  hitlInputValues?: Record<string, string>;
  onHitlInputChange?: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  exploring?: boolean;
}

export function ExplorationTree({
  flow,
  onExplore,
  onCancel,
  onResume,
  hitlInputValues = {},
  onHitlInputChange,
  exploring,
}: ExplorationTreeProps) {
  const status = flow.exploration_status ?? "pending";
  const tree = flow.exploration_tree ?? [];
  const hasTree = tree.length > 0;
  const isPaused = status === "paused_for_hitl";

  // Build the inline HITL node to attach after the last stage
  let hitlNode: React.ReactNode = null;
  if (isPaused && onResume) {
    if (flow.exploration_input_request && onHitlInputChange) {
      hitlNode = (
        <HitlInputForm
          fields={flow.exploration_input_request}
          values={hitlInputValues}
          onChange={onHitlInputChange}
          onSubmit={() => onResume({ values: hitlInputValues })}
        />
      );
    } else if (flow.exploration_confirmation_request) {
      hitlNode = (
        <HitlConfirmation
          summary={flow.exploration_confirmation_request}
          onApprove={() => onResume({ confirmed: true })}
          onDecline={() => onResume({ confirmed: false })}
        />
      );
    } else {
      hitlNode = <HitlAuthWall onResume={() => onResume()} />;
    }
  }

  const lastStageIndex = tree.length - 1;

  return (
    <div className="space-y-3">
      {/* Status header */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 flex-1">
          {status === "exploring" && (
            <>
              <Loader2 className="size-4 animate-spin text-blue-500" />
              <span className="text-sm font-medium text-blue-500">
                Exploring site...
              </span>
            </>
          )}
          {isPaused && (
            <>
              <Hand className="size-4 text-muted-foreground" />
              <span className="text-sm font-medium text-muted-foreground">
                Needs your input
              </span>
            </>
          )}
          {status === "ready" && (
            <>
              <CheckCircle2 className="size-4 text-green-500" />
              <span className="text-sm font-medium text-green-600 dark:text-green-400">
                Exploration complete
              </span>
              <span className="text-xs text-muted-foreground">
                {tree.length} stage{tree.length !== 1 ? "s" : ""} mapped
              </span>
            </>
          )}
          {status === "failed" && (
            <>
              <XCircle className="size-4 text-destructive" />
              <span className="text-sm font-medium text-destructive">
                Exploration failed
              </span>
            </>
          )}
          {status === "cancelled" && (
            <>
              <StopCircle className="size-4 text-muted-foreground" />
              <span className="text-sm font-medium text-muted-foreground">
                Exploration cancelled
              </span>
            </>
          )}
          {status === "pending" && !exploring && (
            <>
              <AlertTriangle className="size-4 text-muted-foreground" />
              <span className="text-sm text-muted-foreground">
                Not yet explored
              </span>
            </>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {onCancel && status === "exploring" && (
            <Button
              variant="ghost"
              size="sm"
              onClick={onCancel}
              className="gap-1.5"
            >
              <StopCircle className="size-3" />
              Stop
            </Button>
          )}
          {onExplore && !isPaused && (
            <Button
              variant={status === "exploring" ? "destructive" : "outline"}
              size="sm"
              onClick={onExplore}
              className="gap-1.5"
            >
              <RefreshCw className="size-3" />
              {status === "exploring" ? "Restart" : hasTree ? "Re-explore" : "Explore"}
            </Button>
          )}
        </div>
      </div>

      {/* Error message */}
      {status === "failed" && flow.exploration_error && (
        <p className="text-xs text-destructive bg-destructive/5 border border-destructive/20 rounded-md px-3 py-2">
          {flow.exploration_error}
        </p>
      )}

      {/* Notes */}
      {flow.exploration_notes && (
        <p className="text-xs text-muted-foreground bg-muted/50 border rounded-md px-3 py-2">
          {flow.exploration_notes}
        </p>
      )}

      {/* Tree */}
      {hasTree && (
        <div className="rounded-lg border bg-card p-2">
          {tree.map((stage, i) => (
            <StageNode
              key={stage.id}
              stage={stage}
              hitlNode={i === lastStageIndex ? hitlNode : undefined}
            />
          ))}
        </div>
      )}

      {/* HITL with no tree yet */}
      {isPaused && !hasTree && hitlNode && (
        <div className="rounded-lg border bg-card p-2">
          {hitlNode}
        </div>
      )}

      {/* Empty state during exploration */}
      {status === "exploring" && !hasTree && (
        <div className="rounded-lg border border-dashed py-8 text-center">
          <Loader2 className="size-6 mx-auto mb-2 text-muted-foreground/40 animate-spin" />
          <p className="text-sm text-muted-foreground">Agent is exploring the site...</p>
          <p className="text-xs text-muted-foreground/60 mt-0.5">
            Stages will appear here as they're discovered
          </p>
        </div>
      )}
    </div>
  );
}
