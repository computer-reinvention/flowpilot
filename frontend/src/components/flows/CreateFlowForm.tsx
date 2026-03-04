import { useState } from "react";
import { ArrowLeft, Plus, X } from "lucide-react";
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
import type { FlowInput } from "@/lib/types";

type InputDefinition = Pick<FlowInput, "name" | "label" | "type" | "placeholder">;

interface CreateFlowFormProps {
  onSubmit: (data: {
    name: string;
    domain: string;
    description: string;
    entry_url?: string;
    inputs?: InputDefinition[];
  }) => Promise<void>;
  onBack: () => void;
  submitting: boolean;
}

const INPUT_TYPES = ["text", "textarea", "number", "select", "date", "time"] as const;

export function CreateFlowForm({
  onSubmit,
  onBack,
  submitting,
}: CreateFlowFormProps) {
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [description, setDescription] = useState("");
  const [entryUrl, setEntryUrl] = useState("");
  const [inputs, setInputs] = useState<InputDefinition[]>([]);

  // New input field being composed
  const [newInputName, setNewInputName] = useState("");
  const [newInputLabel, setNewInputLabel] = useState("");
  const [newInputType, setNewInputType] = useState<InputDefinition["type"]>("text");
  const [newInputPlaceholder, setNewInputPlaceholder] = useState("");

  function addInput() {
    if (!newInputName || !newInputLabel) return;
    setInputs(prev => [
      ...prev,
      {
        name: newInputName,
        label: newInputLabel,
        type: newInputType,
        placeholder: newInputPlaceholder,
      },
    ]);
    setNewInputName("");
    setNewInputLabel("");
    setNewInputType("text");
    setNewInputPlaceholder("");
  }

  function removeInput(index: number) {
    setInputs(prev => prev.filter((_, i) => i !== index));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name || !domain || !description) return;
    onSubmit({
      name,
      domain,
      description,
      entry_url: entryUrl || undefined,
      inputs: inputs.length > 0 ? inputs : undefined,
    });
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="icon" onClick={onBack} aria-label="Go back">
            <ArrowLeft className="size-4" />
          </Button>
          <div>
            <CardTitle>New Flow</CardTitle>
            <CardDescription className="mt-1">
              Create a new browser automation flow
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="flow-name">Name</Label>
            <Input
              id="flow-name"
              placeholder="e.g. Book a ride"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="flow-domain">Domain</Label>
            <Input
              id="flow-domain"
              placeholder="e.g. uber.com"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="flow-description">Description</Label>
            <Textarea
              id="flow-description"
              placeholder="What does this flow automate?"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="flow-entry-url">
              Entry URL{" "}
              <span className="text-muted-foreground font-normal">
                (optional)
              </span>
            </Label>
            <Input
              id="flow-entry-url"
              type="url"
              placeholder="https://example.com/start"
              value={entryUrl}
              onChange={(e) => setEntryUrl(e.target.value)}
            />
          </div>

          {/* Input fields section */}
          <div className="space-y-3">
            <Label>
              Input Fields{" "}
              <span className="text-muted-foreground font-normal">
                (optional)
              </span>
            </Label>

            {/* List of added inputs */}
            {inputs.length > 0 && (
              <div className="space-y-2">
                {inputs.map((inp, index) => (
                  <div
                    key={inp.name}
                    className="flex items-center gap-2 rounded-md border p-2 text-sm"
                  >
                    <span className="font-medium">{inp.label}</span>
                    <span className="text-muted-foreground">({inp.type})</span>
                    {inp.placeholder && (
                      <span className="text-muted-foreground truncate">
                        — {inp.placeholder}
                      </span>
                    )}
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="ml-auto size-6"
                      onClick={() => removeInput(index)}
                      aria-label="Remove input"
                    >
                      <X className="size-3" />
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {/* Add new input form */}
            <div className="rounded-md border p-3 space-y-2">
              <div className="grid grid-cols-2 gap-2">
                <Input
                  placeholder="Field name (e.g. pickup)"
                  value={newInputName}
                  onChange={(e) => setNewInputName(e.target.value)}
                />
                <Input
                  placeholder="Label (e.g. Pickup Location)"
                  value={newInputLabel}
                  onChange={(e) => setNewInputLabel(e.target.value)}
                />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Select
                  value={newInputType}
                  onValueChange={(val) =>
                    setNewInputType(val as InputDefinition["type"])
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {INPUT_TYPES.map((t) => (
                      <SelectItem key={t} value={t}>
                        {t}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Input
                  placeholder="Placeholder text (optional)"
                  value={newInputPlaceholder}
                  onChange={(e) => setNewInputPlaceholder(e.target.value)}
                />
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={addInput}
                disabled={!newInputName || !newInputLabel}
              >
                <Plus className="size-3 mr-1" />
                Add Input
              </Button>
            </div>
          </div>

          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Creating..." : "Create Flow"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
