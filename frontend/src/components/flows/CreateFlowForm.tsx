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

interface InitialInput {
  name: string;
  label: string;
  type: string;
}

interface CreateFlowFormProps {
  onSubmit: (data: {
    name: string;
    domain: string;
    task: string;
    entry_url?: string;
    auth_url?: string;
    auth_before_explore?: boolean;
    inputs?: InitialInput[];
  }) => Promise<void>;
  onBack: () => void;
  submitting: boolean;
}

export function CreateFlowForm({
  onSubmit,
  onBack,
  submitting,
}: CreateFlowFormProps) {
  const [name, setName] = useState("Book an Uber");
  const [domain, setDomain] = useState("uber.com");
  const [task, setTask] = useState("Book a ride on Uber");
  const [entryUrl, setEntryUrl] = useState("https://m.uber.com");
  const [authUrl, setAuthUrl] = useState("");
  const [authBeforeExplore, setAuthBeforeExplore] = useState(false);
  const [inputs, setInputs] = useState<InitialInput[]>([]);

  function addInput() {
    setInputs((prev) => [...prev, { name: "", label: "", type: "text" }]);
  }

  function removeInput(index: number) {
    setInputs((prev) => prev.filter((_, i) => i !== index));
  }

  function updateInput(index: number, field: keyof InitialInput, value: string) {
    setInputs((prev) =>
      prev.map((inp, i) => (i === index ? { ...inp, [field]: value } : inp)),
    );
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name || !domain || !task) return;
    const cleanDomain = domain.replace(/^https?:\/\//i, "").replace(/\/.*$/, "").trim();
    const validInputs = inputs.filter((inp) => inp.name.trim() && inp.type.trim());
    onSubmit({
      name,
      domain: cleanDomain,
      task,
      entry_url: entryUrl || undefined,
      auth_url: authUrl || undefined,
      auth_before_explore: authUrl ? authBeforeExplore : undefined,
      inputs: validInputs.length > 0 ? validInputs : undefined,
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
            <Label htmlFor="flow-task">Task</Label>
            <Textarea
              id="flow-task"
              placeholder="What does this flow automate?"
              value={task}
              onChange={(e) => setTask(e.target.value)}
              rows={3}
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="flow-entry-url">
              Entry URL{" "}
              <span className="text-muted-foreground font-normal">
                (optional — enables auto-exploration)
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

          {/* Auth URL */}
          <div className="space-y-2">
            <Label htmlFor="flow-auth-url">
              Auth URL{" "}
              <span className="text-muted-foreground font-normal">
                (optional — login page for authenticated flows)
              </span>
            </Label>
            <Input
              id="flow-auth-url"
              type="url"
              placeholder="https://auth.example.com/login"
              value={authUrl}
              onChange={(e) => setAuthUrl(e.target.value)}
            />
          </div>

          {/* Auth before explore checkbox — only visible when auth_url is set */}
          {authUrl && (
            <div className="flex items-center gap-2">
              <input
                id="flow-auth-before-explore"
                type="checkbox"
                checked={authBeforeExplore}
                onChange={(e) => setAuthBeforeExplore(e.target.checked)}
                className="size-4 rounded border-input"
              />
              <Label htmlFor="flow-auth-before-explore" className="font-normal cursor-pointer">
                Authenticate before exploration
              </Label>
            </div>
          )}

          {/* Initial Inputs */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>
                Initial Inputs{" "}
                <span className="text-muted-foreground font-normal">(optional)</span>
              </Label>
              <Button type="button" variant="outline" size="sm" className="gap-1" onClick={addInput}>
                <Plus className="size-3" />
                Add
              </Button>
            </div>
            {inputs.map((inp, index) => (
              <div key={index} className="flex items-center gap-2">
                <Input
                  placeholder="name (snake_case)"
                  value={inp.name}
                  onChange={(e) => updateInput(index, "name", e.target.value)}
                  className="flex-1"
                />
                <Input
                  placeholder="Label"
                  value={inp.label}
                  onChange={(e) => updateInput(index, "label", e.target.value)}
                  className="flex-1"
                />
                <Select value={inp.type} onValueChange={(v) => updateInput(index, "type", v)}>
                  <SelectTrigger className="w-28">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="text">Text</SelectItem>
                    <SelectItem value="textarea">Textarea</SelectItem>
                    <SelectItem value="number">Number</SelectItem>
                    <SelectItem value="select">Select</SelectItem>
                    <SelectItem value="date">Date</SelectItem>
                    <SelectItem value="time">Time</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => removeInput(index)}
                  className="shrink-0"
                >
                  <X className="size-3.5" />
                </Button>
              </div>
            ))}
          </div>

          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Creating..." : "Create Flow"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
