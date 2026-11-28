"""单群的在场统计。只保留时间、逻辑人物和计数，不留消息正文。"""

from collections import deque
from dataclasses import dataclass
from math import inf


@dataclass(frozen=True)
class Tuning:
    addressed_quiet: float = 3
    addressed_max: float = 8
    group_quiet: float = 5
    group_max: float = 20
    judge_interval: float = 30
    follow_up_seconds: float = 120
    follow_up_gap_seconds: float = 10
    low_probability: float = 0.05
    medium_probability: float = 0.15
    high_probability: float = 0.3
    question_boost: float = 2
    recent_boost: float = 1.5
    recent_seconds: float = 300
    share_window: float = 600
    half_life: float = 600
    fatigue_start: float = 3
    fatigue_stop: float = 8
    low_scale: float = 0.67
    high_scale: float = 1.5
    pair_rounds: int = 4
    fast_seconds: float = 5
    ignore_wait: float = 180
    backoff_start: float = 900
    backoff_max: float = 7200


class Group:
    def __init__(self, tuning, now):
        self.t = tuning
        self.turns = deque()
        self.fatigue = 0.0
        self.updated = now
        self.last_in = self.last_reply = self.last_judge = -inf
        self.last_follow_up_judge = -inf
        self.replied_to = {}
        # Only inbound references and logical actors. Nothing here survives plugin reload.
        self.recent_senders = deque(maxlen=50)
        self.rounds = 0
        self.partner = None
        self.first_speaker = None
        self.mixed = False
        self.fast = False
        self.quick_answer = False
        self.unanswered = deque()
        self.level = 0
        self.consecutive_misses = 0
        self.backoff_end = -inf
        self.day = None
        self.judgments = self.natural = 0

    def refresh(self, now, day):
        if self.day != day:
            self.day = day
            self.judgments = self.natural = 0
        self.fatigue *= 0.5 ** ((now - self.updated) / self.t.half_life)
        self.updated = now
        self.replied_to = {
            actor: sent
            for actor, sent in self.replied_to.items()
            if now - sent <= self.t.follow_up_seconds
        }
        while self.turns and self.turns[0][0] < now - self.t.share_window:
            self.turns.popleft()
        while self.unanswered and self.unanswered[0] <= now:
            missed = self.unanswered.popleft()
            self.consecutive_misses += 1
            self.level = self.consecutive_misses
            duration = min(self.t.backoff_max, self.t.backoff_start * 2 ** min(self.level - 1, 30))
            self.backoff_end = max(self.backoff_end, missed + duration)
        if now >= self.backoff_end:
            self.level = 0

    def incoming(self, now, actor, engaged):
        self.turns.append((now, actor))
        if self.first_speaker is None:
            self.quick_answer = now - self.last_reply <= self.t.fast_seconds
            self.first_speaker = actor
        elif self.first_speaker != actor:
            self.mixed = True
        if self.partner != actor:
            self.rounds = 0
            self.fast = False
        self.last_in = now
        if self.unanswered:
            self.unanswered.clear()
            self.consecutive_misses = 0
        if engaged:
            self.consecutive_misses = self.level = 0
            self.backoff_end = -inf

    def share(self):
        people = set()
        own = 0
        for _, actor in self.turns:
            if actor is None:
                own += 1
            else:
                people.add(actor)
        return len(people), own / len(self.turns) if self.turns else 0.0

    def follow_up(self, actor):
        if not self.replied_to:
            return "none"
        return "partner" if actor in self.replied_to else "others"

    def sent_part(self, actor, sent):
        if self.updated - sent <= self.t.follow_up_seconds:
            self.replied_to[actor] = max(sent, self.replied_to.get(actor, -inf))

    def replied(self, now, actor, owner, natural, *, sent_at=None):
        self.turns.append((now, None))
        people, share = self.share()
        if share > 2 / (people + 1):
            self.fatigue += 1
        continuation = self.first_speaker is None and self.partner == actor
        if not owner and not self.mixed and (self.first_speaker == actor or continuation):
            if continuation:
                self.rounds += 1
            elif self.partner == actor and now - self.last_reply <= self.t.share_window:
                self.rounds += 1
                self.fast = (
                    self.quick_answer if self.rounds == 2 else self.fast and self.quick_answer
                )
            else:
                self.rounds = 1
                self.fast = False
            self.partner = actor
            if self.rounds >= self.t.pair_rounds:
                self.fatigue += 1.5 if self.fast else 1
        elif self.first_speaker is not None or owner:
            self.rounds = 0
            self.partner = None
            self.fast = False
        self.first_speaker = None
        self.mixed = False
        self.last_reply = now
        if natural:
            self.natural += 1
            sent = now if sent_at is None else sent_at
            deadline = sent + self.t.ignore_wait
            if any(sent < when < deadline and actor is not None for when, actor in self.turns):
                self.consecutive_misses = 0
            else:
                self.unanswered.append(deadline)

    def thresholds(self, band):
        scale = (self.t.low_scale, 1, self.t.high_scale)[band]
        return self.t.fatigue_start * scale, self.t.fatigue_stop * scale

    def willingness(self, band, owner, question, now):
        band = min(2, band + int(owner)) - int(self.level > 0)
        if band < 0:
            return 0
        probability = (self.t.low_probability, self.t.medium_probability, self.t.high_probability)[
            band
        ] * self.fatigue_factor(band)
        if question:
            probability *= self.t.question_boost
        if now - self.last_reply <= self.t.recent_seconds and self.fatigue < self.t.fatigue_start:
            probability *= self.t.recent_boost
        return probability

    def fatigue_factor(self, band):
        start, stop = self.thresholds(band)
        if self.fatigue >= stop:
            return 0
        if self.fatigue <= start:
            return 1
        return 1 - 0.75 * (self.fatigue - start) / (stop - start)
