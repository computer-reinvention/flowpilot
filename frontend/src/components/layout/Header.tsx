import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { User } from "@/lib/types";

interface HeaderProps {
  user: User;
  onLogout: () => void;
}

export function Header({ user, onLogout }: HeaderProps) {
  return (
    <header className="flex items-center justify-between py-4">
      <h1 className="text-xl font-bold text-balance">
        Flow<span className="text-muted-foreground font-normal">Pilot</span>
      </h1>
      <div className="flex items-center gap-3">
        <span className="text-sm text-muted-foreground">{user.name}</span>
        <Button variant="ghost" size="icon" onClick={onLogout} aria-label="Log out">
          <LogOut className="size-4" />
        </Button>
      </div>
    </header>
  );
}
