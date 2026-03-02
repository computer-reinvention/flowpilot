import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Flow, FlowInput } from "@/lib/types";

/** Resolve a stable identifier for a flow input (supports both `name` and legacy `key` fields). */
function inputId(input: FlowInput): string {
  return input.name || input.key || input.label;
}

interface FlowFormProps {
  flow: Flow;
  onSubmit: (values: Record<string, string>) => void;
  onBack: () => void;
  submitting: boolean;
}

function renderInput(
  input: FlowInput,
  value: string,
  onChange: (val: string) => void,
  index: number,
) {
  const id = `flow-input-${index}`;

  switch (input.type) {
    case "textarea":
      return (
        <Textarea
          id={id}
          placeholder={input.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={input.required !== false}
          rows={3}
        />
      );
    case "select":
      return (
        <Select value={value} onValueChange={onChange}>
          <SelectTrigger id={id}>
            <SelectValue placeholder={input.placeholder || "Select..."} />
          </SelectTrigger>
          <SelectContent>
            {(input.options || []).map((opt) => (
              <SelectItem key={opt} value={opt}>
                {opt}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    case "number":
      return (
        <Input
          id={id}
          type="number"
          placeholder={input.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={input.required !== false}
        />
      );
    case "date":
      return (
        <Input
          id={id}
          type="date"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={input.required !== false}
        />
      );
    case "time":
      return (
        <Input
          id={id}
          type="time"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={input.required !== false}
        />
      );
    default:
      return (
        <Input
          id={id}
          type="text"
          placeholder={input.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={input.required !== false}
        />
      );
  }
}

export function FlowForm({ flow, onSubmit, onBack, submitting }: FlowFormProps) {
  // Use array-indexed state — bulletproof, no key collision possible
  const [values, setValues] = useState<string[]>(() =>
    flow.inputs.map(() => "")
  );

  function handleChange(index: number, value: string) {
    setValues(prev => prev.map((v, i) => i === index ? value : v));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    // Map back to named keys for the backend
    const result: Record<string, string> = {};
    flow.inputs.forEach((input, i) => {
      result[inputId(input)] = values[i] ?? "";
    });
    onSubmit(result);
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="icon" onClick={onBack}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <div className="flex items-center gap-2">
              <CardTitle>{flow.name}</CardTitle>
              <Badge variant="secondary">{flow.domain}</Badge>
            </div>
            <CardDescription className="mt-1">
              {flow.description}
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          {flow.inputs.map((input, index) => (
            <div key={index} className="space-y-2">
              <Label htmlFor={`flow-input-${index}`}>
                {input.label}
              </Label>
              {renderInput(input, values[index] ?? "", (val) =>
                handleChange(index, val),
              index)}
            </div>
          ))}
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Starting..." : "Run"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
