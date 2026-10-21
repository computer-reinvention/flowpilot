"""Account/profile keyed private memory; never use caller supplied filesystem names."""
import hashlib
import json
import shutil

from ..repl import UserStore


def input_atoms(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from input_atoms(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from input_atoms(item)
    elif value is not None and str(value):
        yield str(value)


def storage_key(principal, profile_id):
    return hashlib.sha256(json.dumps([principal.account_id, profile_id], separators=(',', ':')).encode()).hexdigest()


class ProfileStore(UserStore):
    def __init__(self, settings, principal, profile_id):
        super().__init__(settings.state_dir, storage_key(principal, profile_id))
        self.retention_days = settings.history_retention_days

    def record_turn(self, task, inputs, result, metrics):
        # Interaction values can appear anywhere in model output. Persist only
        # redacted, bounded context; never store inputs or full tool transcripts.
        def safe(text):
            text = str(text or '')
            for value in sorted(set(input_atoms(inputs)), key=len, reverse=True):
                text = text.replace(value, '[redacted]')
            return text[:4000]
        self.db.execute('INSERT INTO turns(task,inputs,result,metrics) VALUES(?,?,?,?)',
                        (safe(task), '{}', safe(result), json.dumps(metrics)))
        self.db.execute("DELETE FROM turns WHERE created_at < datetime('now', ?)",
                        (f'-{self.retention_days} days',))
        self.db.execute('DELETE FROM turns WHERE id NOT IN (SELECT id FROM turns ORDER BY id DESC LIMIT 100)')
        self.db.commit()


def delete_memory(settings, principal, profile_id):
    store = ProfileStore(settings, principal, profile_id)
    try:
        # Keep the lock inode until deletion completes: competing processes must
        # not be able to open a replacement lock while this profile is removed.
        for child in store.path.iterdir():
            if child.name != 'session.lock':
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    finally:
        store.close()
