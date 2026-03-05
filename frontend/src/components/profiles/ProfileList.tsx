import { Trash2, HardDrive } from "lucide-react";
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
import type { Profile } from "@/hooks/useProfiles";

interface ProfileListProps {
  profiles: Profile[];
  loading: boolean;
  onDelete: (name: string) => void;
}

function formatSize(bytes: number | null): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ProfileList({ profiles, loading, onDelete }: ProfileListProps) {
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

  if (profiles.length === 0) {
    return (
      <div className="border rounded-lg border-dashed py-12 text-center">
        <HardDrive className="size-6 mx-auto mb-2 text-muted-foreground/40" />
        <p className="text-sm text-muted-foreground">No browser profiles saved.</p>
        <p className="text-xs text-muted-foreground/60 mt-1 text-pretty max-w-xs mx-auto">
          Profiles are created automatically when sessions are captured during runs and explorations.
        </p>
      </div>
    );
  }

  return (
    <div>
      <p className="mb-3 text-xs text-muted-foreground tabular-nums">
        {profiles.length} profile{profiles.length !== 1 ? "s" : ""}
      </p>
      <div className="border rounded-lg overflow-hidden divide-y">
        {profiles.map((profile) => (
          <div key={profile.name} className="flex items-center gap-3 px-4 py-2.5">
            <span className="text-sm font-medium truncate">{profile.name}</span>
            {profile.source && (
              <Badge variant="outline" className="text-xs h-5 px-1.5">
                {profile.source}
              </Badge>
            )}
            <span className="text-xs text-muted-foreground tabular-nums">
              {formatSize(profile.size_bytes)}
            </span>
            <span className="text-xs text-muted-foreground tabular-nums ml-auto">
              {formatDate(profile.last_modified)}
            </span>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7 text-muted-foreground hover:text-destructive"
                  aria-label={`Delete profile ${profile.name}`}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle className="text-balance">Delete profile?</AlertDialogTitle>
                  <AlertDialogDescription className="text-pretty">
                    This permanently removes the browser profile <strong>{profile.name}</strong> including all saved cookies and session data.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={() => onDelete(profile.name)}
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
