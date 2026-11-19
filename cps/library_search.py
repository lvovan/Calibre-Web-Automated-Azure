# SPDX-License-Identifier: GPL-3.0-or-later

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise

from .constants import EXTENSIONS_UPLOAD, EXTENSIONS_CONVERT_FROM


_FORMATS = EXTENSIONS_UPLOAD | set(EXTENSIONS_CONVERT_FROM) | {
    'azw4', 'cbc', 'chm', 'fbz', 'pdb', 'pml', 'rb', 'snb', 'tcr', 'txtz',
}
_LEXEME = re.compile(r'"([^"]*)"|([^\s"]+)')
_FORMAT = re.compile(r'\.?([a-z0-9][a-z0-9-]{0,15})', re.ASCII | re.IGNORECASE)
_IRREGULAR = (
    ('child', 'children'), ('man', 'men'), ('woman', 'women'), ('mouse', 'mice'),
    ('person', 'people'), ('cheval', 'chevaux'), ('journal', 'journaux'),
    ('bateau', 'bateaux'), ('jeu', 'jeux'),
)


class SearchSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class SearchTerm:
    text: str
    phrase: bool = False


@dataclass(frozen=True)
class LibrarySearch:
    terms: tuple[SearchTerm, ...]
    book_ids: tuple[int, ...] = ()
    bare_id: int | None = None
    formats: tuple[str, ...] = ()


@lru_cache(maxsize=8192)
def normalize_search_text(value):
    if not value:
        return ''
    folded = unicodedata.normalize('NFKD', value.casefold()).replace('’', "'")
    return ' '.join(''.join(c for c in folded if not unicodedata.combining(c)).split())


def _book_id(value):
    if not re.fullmatch(r'[0-9]{1,19}', value) or not 0 < int(value) <= 2**63 - 1:
        raise SearchSyntaxError('id')
    return int(value)


def parse_library_search(raw):
    if len(raw) > 256:
        raise SearchSyntaxError('length')
    raw = raw.strip()
    if raw.count('"') % 2:
        raise SearchSyntaxError('quote')
    terms = []
    formats = []
    book_ids = []
    bare_id = None
    for token in _LEXEME.finditer(raw):
        phrase, word = token.groups()
        if phrase is not None:
            if not phrase.strip():
                raise SearchSyntaxError('quote')
            terms.append(SearchTerm(normalize_search_text(phrase), True))
        elif word.lower().startswith('id:') or (word.startswith('#') and word[1:].isascii() and word[1:].isdigit()):
            book_ids.append(_book_id(word[3:] if word.lower().startswith('id:') else word[1:]))
        elif word.lower().startswith('format:'):
            match = _FORMAT.fullmatch(word[7:])
            if not match:
                raise SearchSyntaxError('format')
            formats.append(match[1].upper())
        else:
            match = _FORMAT.fullmatch(word)
            if word == raw and match and match[1].lower() in _FORMATS:
                formats.append(match[1].upper())
            else:
                terms.extend(SearchTerm(normalize_search_text(part)) for part in word.split(',') if part)
    if len(terms) + len(formats) > 12:
        raise SearchSyntaxError('length')
    if re.fullmatch(r'[0-9]{1,19}', raw) and 0 < int(raw) <= 2**63 - 1:
        bare_id = int(raw)
    return LibrarySearch(tuple(dict.fromkeys(terms)), tuple(dict.fromkeys(book_ids)),
                         bare_id, tuple(dict.fromkeys(formats)))


def _regular_plural(word):
    if word.endswith(('ss', 'sh', 'ch', 'x', 'z')):
        return word + 'es'
    if re.search(r'[^aeiou]y$', word):
        return word[:-1] + 'ies'
    return word + 's'


def _plural_variants(word):
    for singular, plural in _IRREGULAR:
        if word in (singular, plural):
            return {singular, plural} - {word}
    if not (len(word) >= 3 and word.isalpha()):
        return set()
    if not word.endswith('s') or word.endswith('ss'):
        return {_regular_plural(word)}
    candidates = []
    if word.endswith('ies'):
        candidates.append(word[:-3] + 'y')
    if word.endswith(('sses', 'shes', 'ches', 'xes', 'zes')):
        candidates.append(word[:-2])
    candidates.append(word[:-1])
    for stem in candidates:
        if len(stem) >= 3 and _regular_plural(stem) == word:
            return {stem}
    return set()


@lru_cache(maxsize=4096)
def _matchers(term, phrase):
    def pattern(words):
        return re.compile(r'(?<!\w)(?:' + '|'.join(map(re.escape, sorted(words))) + r')(?!\w)')
    variants = set() if phrase else _plural_variants(term)
    return pattern({term}), pattern(variants) if variants else None


def search_text_score(value, term, phrase, fuzzy=0):
    value = normalize_search_text(value)
    exact, inflected = _matchers(term, phrase)
    if exact.search(value):
        return 2
    if inflected is not None and inflected.search(value) is not None:
        return 1
    if fuzzy and not phrase and fuzzy_term(term) and _fuzzy_match(value, term):
        return FUZZY_SCORE
    return 0


# Typo tolerance: a word within a small edit distance of the term (one edit for 4-7 letters,
# two from 8). Only used when a search finds nothing as typed, and never for quoted phrases.
FUZZY_SCORE = 0.5
_WORD = re.compile(r'\w+')


def fuzzy_term(term):
    return len(term) >= 4 and not term.isdigit()


def _max_edits(term):
    return 1 if len(term) < 8 else 2


def _edit_distance(a, b, limit):
    """Optimal string alignment distance (a transposition counts as one edit), capped at limit + 1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2 = None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            if previous2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


@lru_cache(maxsize=65536)
def _fuzzy_match(value, term):
    limit = _max_edits(term)
    words = _WORD.findall(value)
    # Adjacent words joined catch a missing space ("onepiece"); an exact word start catches a
    # truncated word ("harry pott").
    candidates = words + [a + b for a, b in pairwise(words)]
    return any(word.startswith(term) or _edit_distance(term, word, limit) <= limit for word in candidates)
