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
) {
  const id = `flow-input-${inputId(input)}`;

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
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const input of flow.inputs) {
      initial[inputId(input)] = "";
    }
    return initial;
  });

  function handleChange(name: string, value: string) {
    setValues((prev) => ({ ...prev, [name]: value }));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    onSubmit(values);
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
          {flow.inputs.map((input) => (
            <div key={inputId(input)} className="space-y-2">
              <Label htmlFor={`flow-input-${inputId(input)}`}>
                {input.label}
              </Label>
              {renderInput(input, values[inputId(input)] ?? "", (val) =>
                handleChange(inputId(input), val),
              )}
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
