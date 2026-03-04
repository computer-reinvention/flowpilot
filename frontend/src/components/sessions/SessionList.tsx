import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
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
import { SessionBadge } from "./SessionBadge";
import type { Session } from "@/lib/types";

interface SessionListProps {
  sessions: Session[];
  loading: boolean;
  onDelete: (id: string) => void;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function SessionList({ sessions, loading, onDelete }: SessionListProps) {
  if (loading) {
    return (
      <div className="border rounded-lg overflow-hidden divide-y">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="flex items-center gap-3 px-4 py-3">
            <Skeleton className="h-5 w-28 rounded-full" />
            <Skeleton className="h-3.5 w-16 rounded" />
            <div className="flex-1" />
            <Skeleton className="size-7 rounded" />
          </div>
        ))}
      </div>
    );
  }

  if (sessions.length === 0) {
    return (
      <div className="border rounded-lg border-dashed py-12 text-center">
        <p className="text-sm text-muted-foreground">No saved sessions.</p>
        <p className="text-xs text-muted-foreground/60 mt-1 text-pretty max-w-xs mx-auto">
          Sessions are captured automatically when the agent pauses for login.
        </p>
      </div>
    );
  }

  return (
    <div>
      <p className="mb-3 text-xs text-muted-foreground tabular-nums">
        {sessions.length} session{sessions.length !== 1 ? "s" : ""}
      </p>
      <div className="border rounded-lg overflow-hidden divide-y">
        {sessions.map((session) => (
          <div key={session.id} className="flex items-center gap-3 px-4 py-2.5">
            <SessionBadge session={session} />
            {session.profile_name && session.profile_name !== "default" && (
              <Badge variant="outline" className="text-xs font-mono h-5 px-1.5">
                {session.profile_name}
              </Badge>
            )}
            <span className="text-xs text-muted-foreground tabular-nums ml-auto">
              {formatDate(session.captured_at)}
            </span>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7 text-muted-foreground hover:text-destructive"
                  aria-label={`Delete session for ${session.domain}`}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle className="text-balance">Delete session?</AlertDialogTitle>
                  <AlertDialogDescription className="text-pretty">
                    This removes saved cookies for <strong>{session.domain}</strong>. The agent won't be able to reuse this login session.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={() => onDelete(session.id)}
                    className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                  >
                    Delete
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        ))}
      </div>
    </div>
  );
}
