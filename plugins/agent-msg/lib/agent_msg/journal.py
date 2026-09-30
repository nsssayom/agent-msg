"""Private SQLite message journal; messages and delivery events are append-only."""
import json
import os
from pathlib import Path
import sqlite3

from .protocol import now, validate


def default_path():
    root = Path(os.environ.get('XDG_STATE_HOME', ''))
    if not root.is_absolute():
        root = Path.home() / '.local/state'
    return root / 'agent-msg/journal.db'


class Journal:
    def __init__(self, path=None, readonly=False):
        self.path = Path(path or default_path()).expanduser().absolute()
        self.readonly = readonly
        if self.path.is_symlink():
            raise ValueError('journal path must not be a symlink')
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            try:
                self.connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)
                self.connection.execute('PRAGMA query_only=ON')
                self.connection.execute('SELECT count(*) FROM sqlite_schema').fetchone()
            except sqlite3.OperationalError:
                # Some SQLite builds cannot create missing WAL shared-memory
                # files from a mode=ro handle. mode=rw never creates the main DB;
                # query_only still prohibits all SQL writes to journal contents.
                if hasattr(self, 'connection'):
                    self.connection.close()
                self.connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
                self.connection.execute('PRAGMA query_only=ON')
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.parent.stat().st_uid != os.getuid():
                raise ValueError('journal directory is owned by another user')
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    raise ValueError('journal is owned by another user')
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA busy_timeout=10000')
        self.connection.create_function('casefold', 1, lambda s: (s or '').casefold(), deterministic=True)
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1) or readonly and version != 1:
            self.close()
            raise ValueError(f'unsupported journal schema version: {version}')
        if not readonly:
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.executescript('''
              BEGIN IMMEDIATE;
              CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, in_reply_to TEXT,
                envelope TEXT NOT NULL, observed TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS message_replies ON messages(in_reply_to,seq);
              CREATE INDEX IF NOT EXISTS message_dates ON messages(created_at);
              CREATE TABLE IF NOT EXISTS peers (
                key TEXT PRIMARY KEY, identity TEXT NOT NULL, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, sent INTEGER NOT NULL, received INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS message_state (
                message_id TEXT PRIMARY KEY, status TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS status_counts (status TEXT PRIMARY KEY, total INTEGER NOT NULL);
              CREATE TRIGGER IF NOT EXISTS messages_no_replace BEFORE INSERT ON messages
                WHEN EXISTS (SELECT 1 FROM messages WHERE id=NEW.id OR seq=NEW.seq)
                BEGIN SELECT RAISE(ABORT,'messages are append-only'); END;
              CREATE TABLE IF NOT EXISTS deliveries (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL,
                status TEXT NOT NULL, transport TEXT NOT NULL, created_at TEXT NOT NULL,
                detail TEXT NOT NULL);
              CREATE TRIGGER IF NOT EXISTS deliveries_no_replace BEFORE INSERT ON deliveries
                WHEN EXISTS (SELECT 1 FROM deliveries WHERE seq=NEW.seq)
                BEGIN SELECT RAISE(ABORT,'deliveries are append-only'); END;
              CREATE INDEX IF NOT EXISTS message_deliveries ON deliveries(message_id,seq);
              CREATE TRIGGER IF NOT EXISTS messages_no_update BEFORE UPDATE ON messages
                BEGIN SELECT RAISE(ABORT,'messages are append-only'); END;
              CREATE TRIGGER IF NOT EXISTS messages_no_delete BEFORE DELETE ON messages
                BEGIN SELECT RAISE(ABORT,'messages are append-only'); END;
              CREATE TRIGGER IF NOT EXISTS deliveries_no_update BEFORE UPDATE ON deliveries
                BEGIN SELECT RAISE(ABORT,'deliveries are append-only'); END;
              CREATE TRIGGER IF NOT EXISTS deliveries_no_delete BEFORE DELETE ON deliveries
                BEGIN SELECT RAISE(ABORT,'deliveries are append-only'); END;
              PRAGMA user_version=1;
              COMMIT;
            ''')

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def append(self, envelope, observed, initial_event=None):
        validate(envelope)
        with self.connection:
            self.connection.execute('INSERT INTO messages(id,created_at,in_reply_to,envelope,observed) VALUES (?,?,?,?,?)',
                (envelope['id'], envelope['created_at'], envelope.get('in_reply_to'),
                 json.dumps(envelope, ensure_ascii=False), json.dumps(observed)))
            self.connection.execute('INSERT INTO message_state VALUES (?,?)', (envelope['id'], 'prepared'))
            self.connection.execute("INSERT INTO status_counts VALUES ('prepared',1) ON CONFLICT(status) DO UPDATE SET total=total+1")
            for field in ('from', 'to'):
                peer = envelope[field]
                key = self.peer_key(peer)
                self.connection.execute('''INSERT INTO peers VALUES (?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET identity=excluded.identity,last_seen=excluded.last_seen,
                    sent=peers.sent+excluded.sent,received=peers.received+excluded.received''',
                    (key, json.dumps(peer), envelope['created_at'], envelope['created_at'],
                     int(field == 'from'), int(field == 'to')))
            if initial_event:
                self._event(envelope['id'], **initial_event)
        return envelope['id']

    def event(self, message_id, status, transport, detail=None):
        with self.connection:
            self._event(message_id, status, transport, detail)

    def _event(self, message_id, status, transport, detail=None):
        if status not in {'prepared', 'dispatching', 'sent', 'failed', 'uncertain', 'received'}:
            raise ValueError('invalid delivery status')
        if not self.connection.execute('SELECT 1 FROM messages WHERE id=?', (message_id,)).fetchone():
            raise ValueError('unknown message ID')
        self.connection.execute('INSERT INTO deliveries(message_id,status,transport,created_at,detail) VALUES(?,?,?,?,?)',
            (message_id, status, transport, now(), json.dumps(detail or {})))
        old = self.connection.execute('SELECT status FROM message_state WHERE message_id=?', (message_id,)).fetchone()[0]
        self.connection.execute('UPDATE status_counts SET total=total-1 WHERE status=?', (old,))
        self.connection.execute('INSERT INTO status_counts VALUES (?,1) ON CONFLICT(status) DO UPDATE SET total=total+1', (status,))
        self.connection.execute('UPDATE message_state SET status=? WHERE message_id=?', (status, message_id))

    @staticmethod
    def peer_key(peer):
        return peer['harness'] + ':' + (peer.get('thread_id') or peer.get('name') or 'unknown')

    def _records(self, rows):
        if not rows:
            return []
        ids = [row['id'] for row in rows]
        events = {mid: [] for mid in ids}
        # A bounded page/conversation hydrates its delivery events in one query.
        placeholders = ','.join('?' for _ in ids)
        for event in self.connection.execute(
                f'SELECT message_id,status,transport,created_at,detail FROM deliveries WHERE message_id IN ({placeholders}) ORDER BY seq', ids):
            value = dict(event)
            mid = value.pop('message_id')
            value['detail'] = json.loads(value['detail'])
            events[mid].append(value)
        result = []
        for row in rows:
            value = json.loads(row['envelope'])
            value.update(seq=row['seq'], observed=json.loads(row['observed']), deliveries=events[row['id']])
            value['status'] = value['deliveries'][-1]['status'] if value['deliveries'] else 'prepared'
            result.append(value)
        return result

    def get_message(self, message_id):
        rows = self.connection.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchall()
        records = self._records(rows)
        return records[0] if records else None

    def list_messages(self, limit=100, before_id=None, peer=None, q=None, status=None, before_seq=None):
        clauses, params = [], []
        if before_seq is not None:
            clauses.append('m.seq < ?')
            params.append(int(before_seq))
        if before_id:
            clauses.append('m.seq < (SELECT seq FROM messages WHERE id=?)')
            params.append(before_id)
        if peer:
            clauses.append("(json_extract(envelope,'$.from.harness') || ':' || COALESCE(NULLIF(json_extract(envelope,'$.from.thread_id'),''),NULLIF(json_extract(envelope,'$.from.name'),''),'unknown')=? OR json_extract(envelope,'$.to.harness') || ':' || COALESCE(NULLIF(json_extract(envelope,'$.to.thread_id'),''),NULLIF(json_extract(envelope,'$.to.name'),''),'unknown')=?)")
            params.extend([peer] * 2)
        if q:
            clauses.append("(instr(casefold(json_extract(envelope,'$.msg')),casefold(?))>0 OR instr(casefold(COALESCE(json_extract(envelope,'$.from.name'),'')),casefold(?))>0 OR instr(casefold(COALESCE(json_extract(envelope,'$.to.name'),'')),casefold(?))>0)")
            params.extend([q] * 3)
        if status:
            clauses.append("COALESCE((SELECT status FROM deliveries d WHERE d.message_id=m.id ORDER BY seq DESC LIMIT 1),'prepared')=?")
            params.append(status)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self.connection.execute('SELECT m.* FROM messages m' + where + ' ORDER BY m.seq DESC LIMIT ?',
                                        (*params, max(1, min(int(limit), 1000)))).fetchall()
        return self._records(rows)

    def replies(self, message_id):
        return self._records(self.connection.execute(
            'SELECT * FROM messages WHERE in_reply_to=? ORDER BY seq LIMIT 1000', (message_id,)).fetchall())

    def list_peers(self):
        return [{**json.loads(row['identity']), 'key': row['key'],
                 'first_seen': row['first_seen'], 'last_seen': row['last_seen'],
                 'sent': row['sent'], 'received': row['received']}
                for row in self.connection.execute('SELECT * FROM peers ORDER BY last_seen DESC LIMIT 1000')]

    def get_conversation(self, message_id):
        root = self.get_message(message_id)
        if root is None:
            return []
        seen = {message_id}
        while root.get('in_reply_to') and root['in_reply_to'] not in seen and len(seen) < 1000:
            parent = self.get_message(root['in_reply_to'])
            if parent is None:
                break
            root = parent
            seen.add(root['id'])
        rows = self.connection.execute('''WITH RECURSIVE chain(id) AS (
            SELECT id FROM messages WHERE id=? UNION
            SELECT m.id FROM messages m JOIN chain c ON m.in_reply_to=c.id LIMIT 1000)
            SELECT m.* FROM messages m JOIN chain c ON m.id=c.id ORDER BY m.seq''', (root['id'],)).fetchall()
        return self._records(rows)

    def stats(self):
        counts = dict(self.connection.execute('SELECT status,total FROM status_counts WHERE total>0').fetchall())
        total = sum(counts.values())
        oldest, newest = self.connection.execute('SELECT min(created_at),max(created_at) FROM messages').fetchone()
        return {'total': total, 'messages': total, 'peers': self.connection.execute('SELECT count(*) FROM peers').fetchone()[0], 'by_status': counts,
                'schema_version': 1, 'db_path': str(self.path), 'oldest': oldest, 'newest': newest}
