"""Custom tags of a paper, as a user types them: trimmed, lower case, short, and a limit on how many a paper has."""
import re

MAX_TAGS = 20
MAX_LENGTH = 32
_ALLOWED = re.compile(r"\w[\w \-]*")


def clean(tags: list[str]) -> list[str]:
    """The tags to store, sorted. Blank and repeated tags are dropped; a tag that cannot be stored raises ValueError."""
    out: list[str] = []
    for raw in tags:
        tag = " ".join(raw.split()).lower()
        if not tag:
            continue
        if len(tag) > MAX_LENGTH:
            raise ValueError(f"A tag has at most {MAX_LENGTH} characters: “{tag[:MAX_LENGTH]}…”")
        if not _ALLOWED.fullmatch(tag):
            raise ValueError(f"“{tag}” has characters a tag cannot use. Use letters, digits, spaces, - and _.")
        if tag not in out:
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValueError(f"A paper has at most {MAX_TAGS} tags")
    return sorted(out)
