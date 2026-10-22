"""Account/profile keyed private memory; never use caller supplied filesystem names."""
import fcntl
import hashlib
import json
import os
import re
import shutil
from contextlib import contextmanager

from ..session import UserStore
from ..task_contracts import ErrorCode, TaskFault, task_error


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
        self.db.execute("DELETE FROM turns WHERE created_at < datetime('now', ?)",
                        (f'-{self.retention_days} days',))
        self.db.commit()

    @staticmethod
    def redact(text, inputs, limit=4000):
        text = str(text or '')
        values = sorted(set(input_atoms(inputs)), key=len, reverse=True)
        if values:
            # Single pass: short inputs must not repeatedly expand the redaction
            # marker or redact an earlier replacement into exponential output.
            text = re.sub('|'.join(re.escape(value) for value in values), '[redacted]', text)
        return text[:limit]

    def record_turn(self, task, inputs, result, metrics, *, private_inputs=False):
        # Interaction values can appear anywhere in model output. Persist only
        # redacted, bounded context; never store inputs or full tool transcripts.
        def safe(text):
            return self.redact(text, inputs)
        self.db.execute('INSERT INTO turns(task,inputs,result,metrics) VALUES(?,?,?,?)',
                        (safe(task), '{}', 'Private-input task ended; inspect current website state' if private_inputs else safe(result), json.dumps(metrics)))
        self.db.execute("DELETE FROM turns WHERE created_at < datetime('now', ?)",
                        (f'-{self.retention_days} days',))
        self.db.execute('DELETE FROM turns WHERE id NOT IN (SELECT id FROM turns ORDER BY id DESC LIMIT 100)')
        self.db.commit()


def delete_memory(settings, principal, profile_id, before_delete=None, after_delete=None):
    store = ProfileStore(settings, principal, profile_id)
    try:
        if before_delete:
            before_delete(store.path.name)
        # Keep the lock inode until deletion completes: competing processes must
        # not be able to open a replacement lock while this profile is removed.
        for child in store.path.iterdir():
            if child.name != 'session.lock':
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        if after_delete:
            after_delete(store.path.name)
    finally:
        store.close()


QUARANTINE_LEDGER = 'quarantine-ledger'
PROFILE_KEY = re.compile(r'^[0-9a-f]{64}$')
RESERVED, QUARANTINED, UNKNOWN = 'reserved', 'quarantined', 'unknown'


class QuarantineLedger:
    """Host-wide, fail-closed accounting of retained recovery artifacts.

    One entry per profile key. Every browser execution reserves a worst-case
    slot BEFORE its container exists; a proven save releases it, a failed or
    unproven save converts it into a quarantine. In-flight reservations count
    exactly like quarantines, so a new artifact can never push usage past the
    configured quotas. Exhaustion refuses new work; this class never deletes a
    container, archive or recovery marker. Entries live beside (not inside)
    profile state and task history, so history pruning cannot drop them; only
    a proven save or explicit profile deletion removes one. Unreadable entries
    and unattributed artifacts are charged conservatively. Construction is
    I/O-free and no lock is held across Docker or archive I/O.
    """

    def __init__(self, settings):
        self.settings = settings
        self.root = settings.state_dir / QUARANTINE_LEDGER

    @contextmanager
    def _locked(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(self.root / '.lock', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _path(self, key):
        if not PROFILE_KEY.fullmatch(key):
            raise ValueError('Invalid profile key')
        return self.root / f'{key}.json'

    def _entries(self):
        entries = {}
        for path in self.root.glob('*.json'):
            if not PROFILE_KEY.fullmatch(path.stem):
                continue
            try:
                entry = json.loads(path.read_text())
                if entry.get('state') not in (RESERVED, QUARANTINED):
                    raise ValueError
            except (OSError, ValueError, AttributeError):
                # Never treat an unreadable record as free capacity.
                entry = {'state': UNKNOWN, 'account_id': None}
            entries[path.stem] = entry
        return entries

    def _write(self, key, entry):
        path = self._path(key)
        temporary = path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(entry, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)

    def charge(self, entry):
        measured = entry.get('measured_bytes')
        reserved = self.settings.quarantine_reserve_bytes
        # Unknown size keeps the full worst-case reservation; a larger
        # measurement is charged as measured. Never under-account.
        if isinstance(measured, int) and not isinstance(measured, bool) and measured >= 0:
            return max(reserved, measured)
        return reserved

    def _usage(self, entries, account_id):
        # Unattributed artifacts count against every account (fail closed).
        mine = [entry for entry in entries.values() if entry.get('account_id') in (account_id, None)]
        return {'count': len(entries), 'bytes': sum(self.charge(entry) for entry in entries.values()),
                'account_count': len(mine), 'account_bytes': sum(self.charge(entry) for entry in mine)}

    def usage(self, account_id=None):
        with self._locked():
            return self._usage(self._entries(), account_id)

    def entries(self):
        with self._locked():
            return self._entries()

    def reserve(self, lease, key):
        """Atomically reserve one worst-case artifact slot or refuse (typed)."""
        settings = self.settings
        with self._locked():
            entries = self._entries()
            existing = entries.get(key)
            if existing is not None:
                if existing['state'] == RESERVED:
                    raise TaskFault(task_error(ErrorCode.CONFLICT, 'Profile already has an active browser reservation'))
                raise TaskFault(task_error(ErrorCode.PROFILE_RECOVERY_REQUIRED, 'Profile requires recovery'))
            used = self._usage(entries, lease.principal.account_id)
            slot = settings.quarantine_reserve_bytes
            if (used['count'] + 1 > settings.quarantine_max_count
                    or used['bytes'] + slot > settings.quarantine_max_bytes
                    or used['account_count'] + 1 > settings.quarantine_max_account_count
                    or used['account_bytes'] + slot > settings.quarantine_max_account_bytes):
                raise TaskFault(task_error(
                    ErrorCode.CAPACITY_EXHAUSTED,
                    'Recovery quarantine quota is full; resolve or delete retained profiles before new work'))
            self._write(key, {'state': RESERVED, 'account_id': lease.principal.account_id,
                              'task_id': lease.task_id, 'lease_generation': lease.generation})
        return key

    @staticmethod
    def _owned(entry, lease):
        return (entry.get('state') == RESERVED and entry.get('account_id') == lease.principal.account_id
                and entry.get('task_id') == lease.task_id and entry.get('lease_generation') == lease.generation)

    def release(self, key, lease):
        """Free only this execution's own reservation, after a proven save."""
        with self._locked():
            entry = self._entries().get(key)
            if entry is not None and self._owned(entry, lease):
                self._path(key).unlink()
                return True
            return False

    def quarantine(self, key, lease, measured_bytes=None):
        """Convert this execution's reservation into a retained quarantine."""
        with self._locked():
            entry = self._entries().get(key)
            if entry is not None and not self._owned(entry, lease):
                return False  # never overwrite another record or an existing quarantine
            self._write(key, {'state': QUARANTINED, 'account_id': lease.principal.account_id,
                              'task_id': lease.task_id, 'lease_generation': lease.generation,
                              'measured_bytes': measured_bytes})
            return True

    def forget(self, key):
        """Explicit owner-authorized profile deletion, after its artifacts are gone."""
        with self._locked():
            self._path(key).unlink(missing_ok=True)

    def reconcile(self, held=(), attribution=None):
        """Recovery accounting. Filesystem only; never deletes recovery artifacts.

        A dead execution's stale reservation becomes a quarantine when its
        profile retains a recovery marker and is dropped otherwise (nothing was
        retained). Markers without an entry (older releases or a lost ledger)
        are adopted. Unknown owners stay unattributed and are charged to all.
        Cost is one directory scan of the state root per call.
        """
        attribution = attribution or {}
        state_dir = self.settings.state_dir
        adopted = []
        with self._locked():
            entries = self._entries()
            for key, entry in entries.items():
                if key in held:
                    continue
                if entry['state'] == RESERVED:
                    if (state_dir / key / 'recovery-required').exists():
                        entry = {**entry, 'state': QUARANTINED, 'measured_bytes': None}
                        self._write(key, entry)
                    else:
                        self._path(key).unlink()
                        continue
                if entry['state'] == QUARANTINED and entry.get('account_id') is None and key in attribution:
                    self._write(key, {**entry, 'account_id': attribution[key]})
            for directory in state_dir.iterdir() if state_dir.is_dir() else ():
                key = directory.name
                if (not PROFILE_KEY.fullmatch(key) or key in entries or directory.is_symlink()
                        or not (directory / 'recovery-required').is_file()):
                    continue
                try:
                    marker = json.loads((directory / 'recovery-required').read_text())
                    if not isinstance(marker, dict):
                        raise ValueError
                except (OSError, ValueError):
                    marker = {}
                account = marker.get('account_id')
                self._write(key, {'state': QUARANTINED,
                                  'account_id': account if isinstance(account, str) else attribution.get(key),
                                  'task_id': marker.get('task_id'), 'lease_generation': marker.get('lease_generation'),
                                  'measured_bytes': None})
                adopted.append(key)
        return adopted
