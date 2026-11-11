# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Owner actions: the one way a Chromatic Manager job says "this needs you, now".

Everything the person at the console has to do while a job runs (switch the console off and on,
plug it in, run ModRetro's updater, answer a question in a small window) is shown big and bright on
the window's main screen, with a countdown when it is timed. The log is only a record: a request
shown in the log alone is easy to miss, and the job would time out.

A job announces it here and nowhere else:

    owner.need(title, instruction, seconds=None, log=None)   the owner must do this now
    owner.done(note='', log=None)                            it was done: "Done, thank you"
    owner.missed(next_step, log=None)                        the time ran out: what to do next
    owner.close()                                            no longer needed (an answered question)

`need` also writes the request through the job's own `say` (the same line the log always had; pass
`log` to keep an existing wording, or '' when the caller logs it itself). Listeners (the Manager
service, which hands it to the window and the local API) get one small dictionary per change:

    {'id', 'kind', 'state', 'title', 'instruction', 'seconds', 'note', 'next'}

with `state` one of waiting / done / timeout / closed and `seconds` the time allowed (None: no time
limit). Nothing here opens a device, reads a private file or decides anything: it only announces.
The text menu and the command line have no listener, so there the printed line is the request."""

WAITING, DONE, TIMEOUT, CLOSED = 'waiting', 'done', 'timeout', 'closed'
ACTION, QUESTION = 'action', 'question'
THANKS = 'Done, thank you'
TIMED_OUT = 'Time ran out'
# What the window shows while a job's question waits in its small window.
QUESTION_TITLE = 'Answer the question'
QUESTION_TEXT = 'A small window is asking you something. Answer it there and this job goes on.'


class OwnerPrompts:
    """One owner action at a time, per job. Never raises into the job: a listener that fails is
    ignored (showing a request must never stop or change the work)."""

    def __init__(self, say=print):
        self.say = say
        self.listeners = []
        self.current = None
        self.count = 0
        self.history = []  # every change, in order (small dictionaries): for tests and reports

    def listen(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)
        return listener

    @property
    def waiting(self):
        return self.current is not None and self.current['state'] == WAITING

    def _tell(self):
        event = dict(self.current)
        self.history.append(event)
        for listener in list(self.listeners):
            try:
                listener(dict(event))
            except Exception:
                pass

    def need(self, title, instruction, seconds=None, log=None, kind=ACTION):
        """The owner must do this now. `seconds`: how long the job waits for it (None: until it is
        done). Returns the request's number."""
        if self.waiting:
            self.close()
        self.count += 1
        self.current = {'id': self.count, 'kind': kind, 'state': WAITING, 'title': str(title),
                        'instruction': str(instruction),
                        'seconds': None if seconds is None else max(0.0, float(seconds)), 'note': '', 'next': ''}
        text = f'{title}. {instruction}' if log is None else log
        if text:
            self.say(text)
        self._tell()
        return self.count

    def _end(self, state, note='', next_step='', log=None):
        if not self.waiting:
            return False
        self.current = dict(self.current, state=state, note=str(note or ''), next=str(next_step or ''))
        if log:
            self.say(log)
        self._tell()
        return True

    def done(self, note='', log=None):
        """The owner did it (the job saw it happen)."""
        return self._end(DONE, note=note, log=log)

    def missed(self, next_step, log=None):
        """The time ran out. `next_step`: the plain next thing to do, shown in red."""
        return self._end(TIMEOUT, next_step=next_step, log=log)

    def close(self):
        """Not needed any more (a question that was answered, or the job ended another way)."""
        return self._end(CLOSED)
