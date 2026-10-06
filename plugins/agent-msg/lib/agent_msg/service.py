"""Journal first, dispatch once, and route correlated replies."""
from pathlib import Path
import time
import uuid

from . import transports
from .identity import identify_sender, observe_process
from .protocol import make, wire_text


def current_sender():
    observed = observe_process()
    report = transports.discover_agents()
    observed['warnings'].extend(report['warnings'])
    return identify_sender(observed, report['agents']), observed


def reply_route(journal, message_id, sender, wait):
    return {'kind': 'agent-msg', 'request_id': message_id,
            'notify': sender if not wait and sender.get('thread_id') else None}


def dispatch(journal, envelope, observed, *, notify=True):
    if not notify:
        journal.append(envelope, observed, initial_event={
            'status': 'received', 'transport': 'journal', 'detail': {'meaning': 'reply committed to local journal'}})
        return journal.get_message(envelope['id'])
    target = envelope['to']
    journal.append(envelope, observed, initial_event={'status': 'dispatching', 'transport': target['harness'],
                   'detail': {'pid': observed['pid'], 'started_at_epoch': observed.get('started_at_epoch')}})
    try:
        receipt = transports.send(target, wire_text(envelope), envelope['id'])
    except transports.NotDispatched as error:
        journal.event(envelope['id'], 'failed', target['harness'], {'error': str(error), 'retry_safe': True})
        raise
    except BaseException as error:
        # Socket/RPC failures can occur after delivery. Persist uncertainty, never
        # auto-retry a possibly delivered instruction. Prepared rows are durable.
        journal.event(envelope['id'], 'uncertain', target['harness'], {'error': str(error), 'retry_safe': False})
        raise
    journal.event(envelope['id'], 'sent', receipt['transport'], receipt)
    return journal.get_message(envelope['id'])


def send_message(journal, target, text, *, cwd=None, wait=False):
    agent = transports.resolve_target(target, cwd or str(Path.cwd()))
    sender, observed = current_sender()
    if (sender['harness'], sender.get('thread_id')) == (agent['harness'], agent['thread_id']):
        raise ValueError('refusing to send a message to the invoking agent itself')
    mid = str(uuid.uuid4())
    envelope = make(sender, transports.agent_identity(agent), text, reply_route(journal, mid, sender, wait), message_id=mid)
    return dispatch(journal, envelope, observed)


def reply_message(journal, message_id, text, *, wait=False):
    original = journal.get_message(message_id)
    if original is None:
        raise ValueError('unknown message ID in this journal')
    sender, observed = current_sender()
    # Fail closed if an attributable different agent tries to reply to a peer's
    # request. This catches accidental use; same-user DB access is not a boundary.
    recipient = original['to']
    if sender.get('thread_id') and (sender['harness'], sender['thread_id']) != (recipient['harness'], recipient.get('thread_id')):
        raise ValueError('this message is addressed to another agent; inspect it with show instead')
    if not sender.get('thread_id'):
        observed['identity_note'] = 'Invoking process could not be attributed to a harness; reply identity remains unknown.'
    destination = original['from']
    notify = original['reply_route'].get('notify')
    if notify and (notify.get('harness'), notify.get('thread_id')) != (destination['harness'], destination.get('thread_id')):
        raise ValueError('reply route does not match original sender')
    if notify and (not destination.get('cwd') or not recipient.get('cwd')):
        raise ValueError('reply requires verified working directories')
    # The original journaled request authorizes replying to its sender, even
    # across workdirs. Each transport re-resolves the exact sender ID AND its
    # recorded cwd before dispatch; never substitute the responder's cwd.
    mid = str(uuid.uuid4())
    envelope = make(sender, destination, text, reply_route(journal, mid, sender, wait), message_id=mid, in_reply_to=message_id)
    return dispatch(journal, envelope, observed, notify=bool(notify))


def wait_reply(journal, message_id, timeout):
    original = journal.get_message(message_id)
    if original is None:
        raise ValueError('unknown message ID')
    expected = original['to']
    deadline = time.monotonic() + timeout
    while True:
        replies = journal.replies(message_id)
        for reply in replies:
            sender = reply['from']
            if (sender['harness'], sender.get('thread_id')) == (expected['harness'], expected.get('thread_id')):
                return reply
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.2, remaining))
