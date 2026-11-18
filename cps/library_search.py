# SPDX-License-Identifier: GPL-3.0-or-later

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

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


def search_text_score(value, term, phrase):
    value = normalize_search_text(value)
    exact, inflected = _matchers(term, phrase)
    if exact.search(value):
        return 2
    return int(inflected is not None and inflected.search(value) is not None)
