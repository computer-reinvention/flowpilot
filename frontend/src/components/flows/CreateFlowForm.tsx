import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

interface CreateFlowFormProps {
  onSubmit: (data: {
    name: string;
    domain: string;
    task: string;
    entry_url?: string;
  }) => Promise<void>;
  onBack: () => void;
  submitting: boolean;
}

export function CreateFlowForm({
  onSubmit,
  onBack,
  submitting,
}: CreateFlowFormProps) {
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [task, setTask] = useState("");
  const [entryUrl, setEntryUrl] = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name || !domain || !task) return;
    onSubmit({
      name,
      domain,
      task,
      entry_url: entryUrl || undefined,
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

          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Creating..." : "Create Flow"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
