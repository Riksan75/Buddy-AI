"""
Buddy OS — Intent Gate (v8.6)
=============================
Stops Buddy from mistaking CONVERSATION for a WORK ORDER.

The old triggers fired on bare keywords: any message containing "tiktok" or
matching "make.*video" launched a 3-video shorts batch. So a message like

    "i want to make you an AI who will run youtube channels, IG, Tiktok ...
     later we can get better hardware"

...was read as "produce 3 shorts about 'your work'" — GPU time burned on
garbage while the boss was only explaining the plan.

`classify_action(text)` separates the two by SCORING intent instead of
keyword-matching:

    kind == 'command'    explicit slash command — always run it
    kind == 'produce'    a real work order — run it
    kind == 'ambiguous'  might be either — ASK, never spend GPU on a guess
    kind == 'discuss'    talking about the work, not ordering it — just talk

The rule of thumb it encodes: a work order names an ACTION on an ASSET
("make 3 shorts about dog training"). Talk about capabilities, plans,
hardware, the future, or about Buddy himself is not an order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# an imperative production verb ...
VERB = r'(?:make|create|generate|produce|build|render|film|shoot|post|upload|publish|cut|edit)'
# ... acting on a media asset
ASSET = r'(?:shorts?|videos?|reels?|tiktoks?|clips?|episodes?|campaigns?|posts?)'

# Phrases that mean "we are TALKING about the work" — each one vetoes.
DISCUSS_MARKERS = [
    r'\bi want to (?:make|turn|teach|train|build) you\b',
    r'\b(?:make|turn|teach|train|build) you (?:an?|into|to)\b',
    r'\bteach you\b', r'\bunderstand\b', r'\byou will\b', r'\byou should\b',
    r'\byour job\b', r'\byour work\b', r'\bour setup\b', r'\bdid you\b',
    r'\blater\b', r'\bin (?:the )?future\b', r'\bsomeday\b',
    r'\bif you learn\b', r'\bonce we\b', r'\bwhen we (?:get|have)\b',
    r'\bwe can\b', r'\bi will\b', r'\bwhat do you think\b',
    r'\bexplain\b', r'\bhow much\b', r'\bcan we earn\b', r'\bstrategy\b',
    r'\bthe plan\b', r'\bhardware\b', r'\bwhich is just\b',
]

# Confirmations that mean "yes, run the thing you just asked me about".
YES = re.compile(r'^\s*(?:yes|yep|yeah|ya|do it|go|go ahead|run it|confirm|'
                 r'proceed|ok|okay|haan|kar do|chalo)\b[\s.!]*$', re.I)


@dataclass
class Intent:
    kind: str                       # command | produce | ambiguous | discuss
    score: int = 0
    count: Optional[int] = None     # "3 shorts" -> 3
    topic: str = ''
    reasons: List[str] = field(default_factory=list)

    @property
    def is_order(self) -> bool:
        return self.kind in ('command', 'produce')


def _bad_topic(topic: str) -> bool:
    """True when the extracted topic is a pronoun/filler, not a subject."""
    t = (topic or '').strip().lower().strip('.!?,"\'')
    if len(t) < 3:
        return True
    return t in {
        'your work', 'your job', 'you', 'me', 'us', 'it', 'this', 'that',
        'them', 'my work', 'the work', 'work', 'money', 'earn money',
        'our setup', 'the plan', 'everything', 'anything', 'something',
    }


def classify_action(text: str) -> Intent:
    """Score a message as command / produce / ambiguous / discuss."""
    raw = (text or '').strip()
    low = raw.lower()
    if low.startswith('/'):
        return Intent('command', 99, reasons=['explicit slash command'])

    score, reasons = 0, []

    if re.match(rf'^(?:please\s+|pls\s+|now\s+)?{VERB}\b', low):
        score += 3
        reasons.append('starts with a production verb')
    if re.match(rf'^(?:can|could|would) you\s+(?:please\s+)?{VERB}\b', low):
        score += 3
        reasons.append('direct request to produce')

    m = re.search(rf'\b(\d+)\s*{ASSET}\b', low)
    count = int(m.group(1)) if m else None
    if m:
        score += 3
        reasons.append(f'explicit quantity ({m.group(1)})')

    # verb and asset close together ("generate a reel", "post 2 clips")
    if re.search(rf'\b{VERB}\b[^.!?]{{0,40}}\b{ASSET}\b', low):
        score += 2
        reasons.append('verb acting on an asset')

    if re.search(r'\babout\s+[a-z0-9]', low) and re.search(rf'\b{ASSET}\b', low):
        score += 1
        reasons.append('names a subject with "about"')

    vetoes = [p for p in DISCUSS_MARKERS if re.search(p, low)]
    if vetoes:
        score -= 4
        reasons.append(f'discussion marker: {vetoes[0]}')

    if len(raw) > 240 and count is None:
        score -= 2
        reasons.append('long message, no quantity — reads as strategy talk')

    if low.count('?') and not re.search(rf'^(?:can|could|would|will) you\s+(?:please\s+)?{VERB}', low):
        score -= 1
        reasons.append('question, not an instruction')

    topic = ''
    # take the LAST "about ..." so "a video about drone photography" yields the
    # subject rather than "a video about drone photography"
    marks = [m.end() for m in
             re.finditer(r'\b(?:about|on the topic of|regarding)\s+', raw, re.I)]
    if marks:
        topic = raw[marks[-1]:].strip()

    if score >= 3:
        kind = 'produce'
    elif score >= 1:
        kind = 'ambiguous'
    else:
        kind = 'discuss'

    # a work order with no usable subject is not actionable — ask
    if kind == 'produce' and topic and _bad_topic(topic):
        kind = 'ambiguous'
        reasons.append(f'topic "{topic}" is filler, not a subject')
    if kind == 'produce' and not topic and count is None:
        kind = 'ambiguous'
        reasons.append('no subject and no quantity — nothing concrete to make')

    return Intent(kind, score, count, topic, reasons)


def is_confirmation(text: str) -> bool:
    return bool(YES.match(text or ''))


# Words that make a message *look* like media work.
MEDIA_WORDS = re.compile(r'\b(shorts?|tiktoks?|reels?|videos?|clips?|episodes?)\b', re.I)


def clean_topic(topic: str) -> str:
    return (topic or '').strip().strip(' .!?,"\'')


def gate_media(text: str):
    """Single decision point for every media-production trigger site.

    Returns (decision, intent):
        'launch'  a real order with a usable subject — produce now
        'ask'     might be an order — propose the exact command, wait for "yes"
        'pass'    not a media order — let normal chat / mission handling run
    """
    intent = classify_action(text)
    low = (text or '').lower()
    if intent.kind == 'command':            # slash commands route themselves
        return 'pass', intent
    if not MEDIA_WORDS.search(low):
        return 'pass', intent
    if intent.kind == 'produce' and clean_topic(intent.topic):
        return 'launch', intent
    if intent.kind in ('produce', 'ambiguous'):
        return 'ask', intent
    return 'pass', intent


if __name__ == '__main__':
    CASES = [
        # (text, expected kind)
        ('/video top 5 AI tools', 'command'),
        ('make 3 shorts about dog training', 'produce'),
        ('create a reel about morning routines', 'produce'),
        ('can you make 5 videos about budget laptops?', 'produce'),
        ('post 2 clips from my podcast', 'produce'),
        ('i want to make you an AI who will run youtube chanels, IG Chanels, '
         'Tiktok, chanels, facebook group, to earn money...did you our setup...'
         'it has Earned today 100 to 10000 Dollars per target....if you learn '
         'all those skills which i have integrate into you...later we can get '
         'better Hardware to give you more power...for your work', 'discuss'),
        ('this i want to teach you to understand which is just word or command '
         'you to do any specific work', 'discuss'),
        ('what do you think about tiktok as a channel?', 'discuss'),
        ('how much can we earn from youtube shorts?', 'discuss'),
        ('later we can make videos for instagram', 'discuss'),
        ('videos', 'discuss'),
        ('make videos', 'ambiguous'),
    ]
    bad = 0
    for text, want in CASES:
        got = classify_action(text)
        ok = got.kind == want
        bad += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} [{got.kind:9s} want {want:9s} "
              f"score {got.score:+d}] {text[:60]!r}")
        if not ok:
            print(f'       reasons: {got.reasons}')
    print(f'\n{len(CASES)-bad}/{len(CASES)} correct')

    GATE = [
        # (text, expected decision at a media trigger site)
        ('make 3 shorts about dog training', 'launch'),
        ('create a reel about morning routines', 'launch'),
        ('make videos', 'ask'),
        ('post 2 clips from my podcast', 'ask'),      # no subject -> confirm first
        ('i want to make you an AI who will run youtube chanels, IG Chanels, '
         'Tiktok, chanels, facebook group, to earn money...later we can get '
         'better Hardware to give you more power...for your work', 'pass'),
        ('this i want to teach you to understand which is just word or command '
         'you to do any specific work', 'pass'),
        ('how much can we earn from youtube shorts?', 'pass'),
        ('/video top 5 AI tools', 'pass'),            # command owns its routing
        ('what is the weather like', 'pass'),
    ]
    for text, want in GATE:
        got, it = gate_media(text)
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} gate={got:6s} want={want:6s} "
              f"({it.kind}) {text[:50]!r}")

    assert is_confirmation('yes') and is_confirmation('do it') \
        and not is_confirmation('yes but make 3 shorts')
    print('confirmation detector OK')
    raise SystemExit(1 if bad else 0)
