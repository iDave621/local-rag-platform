"""Stable document identity derived from the original name, never a staging path."""
import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class VersionIdentity:
    topic_key: str
    topic_name: str
    version_rank: int
    version_label: str


# Lookarounds preserve separators between two adjacent version markers.
_VERSION = re.compile(
    r'(?i)(?<![\w])(?:v(?:ersion)?|rev(?:ision)?)[_\-.\s]*(\d+)(?!\w)'
)
_VERSION_DIRECTORY = re.compile(r'(?i)(?:(?:v(?:ersion)?|rev(?:ision)?)[_\-. ]*)?\d+')


def identify_version(filename, parent=''):
    filename = str(filename).replace('\\', '/')
    parent = str(parent).replace('\\', '/')
    stem = PurePosixPath(filename).stem
    searchable = f'{parent}/{stem}'.replace('_', ' ')
    ranks = [int(match.group(1)) for match in _VERSION.finditer(searchable)]
    rank = max(ranks, default=0)
    cleaned = _VERSION.sub(' ', stem.replace('_', ' '))
    cleaned = re.sub(r'[_\-.]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().casefold()
    parts = [
        part.casefold() for part in PurePosixPath(parent).parts
        if part not in {'/', '.', '..'} and not _VERSION_DIRECTORY.fullmatch(part)
    ]
    topic_name = cleaned or stem.casefold()
    return VersionIdentity(
        '/'.join([*parts, topic_name]), topic_name, rank,
        f'v{rank}' if rank else 'unversioned',
    )
