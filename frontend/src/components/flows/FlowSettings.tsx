import { useState } from "react";
import { Plus, X } from "lucide-react";
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
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import type { Flow, FlowInput } from "@/lib/types";

type InputDefinition = Pick<FlowInput, "name" | "label" | "type" | "placeholder">;

interface FlowSettingsProps {
  flow: Flow;
  onUpdate: (flowId: string, updates: Record<string, unknown>) => Promise<Flow>;
  onDelete: (flowId: string) => Promise<void>;
  onDeleted: () => void;
}

const INPUT_TYPES = ["text", "textarea", "number", "select", "date", "time"] as const;

export function FlowSettings({ flow, onUpdate, onDelete, onDeleted }: FlowSettingsProps) {
  const [name, setName] = useState(flow.name);
  const [domain, setDomain] = useState(flow.domain);
  const [task, setTask] = useState(flow.task);
  const [entryUrl, setEntryUrl] = useState(flow.entry_url ?? "");
  const [inputs, setInputs] = useState<InputDefinition[]>(
    flow.inputs.map((i) => ({ name: i.name, label: i.label, type: i.type, placeholder: i.placeholder }))
  );
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // New input field
  const [newInputName, setNewInputName] = useState("");
  const [newInputLabel, setNewInputLabel] = useState("");
  const [newInputType, setNewInputType] = useState<InputDefinition["type"]>("text");
  const [newInputPlaceholder, setNewInputPlaceholder] = useState("");

  function addInput() {
    if (!newInputName || !newInputLabel) return;
    setInputs((prev) => [...prev, { name: newInputName, label: newInputLabel, type: newInputType, placeholder: newInputPlaceholder }]);
    setNewInputName("");
    setNewInputLabel("");
    setNewInputType("text");
    setNewInputPlaceholder("");
  }

  function removeInput(index: number) {
    setInputs((prev) => prev.filter((_, i) => i !== index));
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!name || !domain || !task) return;
    setSaving(true);
    try {
      await onUpdate(flow.id, {
        name,
        domain,
        task,
        entry_url: entryUrl || null,
        inputs: inputs.length > 0 ? inputs : [],
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    try {
      await onDelete(flow.id);
      onDeleted();
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-8">
      <form onSubmit={handleSave} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="fs-name">Name</Label>
          <Input id="fs-name" value={name} onChange={(e) => setName(e.target.value)} required />
        </div>
        <div className="space-y-2">
          <Label htmlFor="fs-domain">Domain</Label>
          <Input id="fs-domain" value={domain} onChange={(e) => setDomain(e.target.value)} required />
        </div>
        <div className="space-y-2">
          <Label htmlFor="fs-task">Task</Label>
          <Textarea id="fs-task" value={task} onChange={(e) => setTask(e.target.value)} rows={3} required />
        </div>
        <div className="space-y-2">
          <Label htmlFor="fs-entry-url">
            Entry URL <span className="text-muted-foreground font-normal">(optional)</span>
          </Label>
          <Input id="fs-entry-url" type="url" placeholder="https://example.com/start" value={entryUrl} onChange={(e) => setEntryUrl(e.target.value)} />
        </div>

        {/* Input fields */}
        <div className="space-y-3">
          <Label>Input Fields</Label>
          {inputs.length > 0 && (
            <div className="space-y-2">
              {inputs.map((inp, index) => (
                <div key={`${inp.name}-${index}`} className="flex items-center gap-2 rounded-md border p-2 text-sm">
                  <span className="font-medium">{inp.label}</span>
                  <span className="text-muted-foreground">({inp.type})</span>
                  {inp.placeholder && <span className="text-muted-foreground truncate">— {inp.placeholder}</span>}
                  <Button type="button" variant="ghost" size="icon" className="ml-auto size-6" onClick={() => removeInput(index)}>
                    <X className="size-3" />
                  </Button>
                </div>
              ))}
            </div>
          )}
          <div className="rounded-md border p-3 space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <Input placeholder="Field name" value={newInputName} onChange={(e) => setNewInputName(e.target.value)} />
              <Input placeholder="Label" value={newInputLabel} onChange={(e) => setNewInputLabel(e.target.value)} />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <Select value={newInputType} onValueChange={(val) => setNewInputType(val as InputDefinition["type"])}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {INPUT_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
                </SelectContent>
              </Select>
              <Input placeholder="Placeholder (optional)" value={newInputPlaceholder} onChange={(e) => setNewInputPlaceholder(e.target.value)} />
            </div>
            <Button type="button" variant="outline" size="sm" onClick={addInput} disabled={!newInputName || !newInputLabel}>
              <Plus className="size-3 mr-1" /> Add Input
            </Button>
          </div>
        </div>

        <Button type="submit" disabled={saving} className="w-full">
          {saved ? "Saved!" : saving ? "Saving..." : "Save Changes"}
        </Button>
      </form>

      {/* Danger zone */}
      <div className="rounded-lg border border-destructive/20 p-4 space-y-3">
        <div>
          <h3 className="text-sm font-medium text-destructive">Danger Zone</h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            Permanently delete this flow and all its trigger hooks.
          </p>
        </div>
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button variant="destructive" size="sm" disabled={deleting}>
              {deleting ? "Deleting..." : "Delete Flow"}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete Flow</AlertDialogTitle>
              <AlertDialogDescription>
                Delete <span className="font-medium text-foreground">"{flow.name}"</span>? This action cannot be undone. All trigger hooks for this flow will also be removed.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={handleDelete} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  );
}
