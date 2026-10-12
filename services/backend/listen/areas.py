"""Research areas: a fixed list of broad fields that every paper is placed in (see worker/classify.py).

The list lives in code so that the LLM can only choose from it and the screens can rely on the ids. A paper has
one primary area and up to two more; "other" is used only when nothing else fits. Rules are the fallback when
the LLM is not available: they count the cue words of each area in the title, abstract, keywords and sections.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OTHER = "other"

# id, name, cue words (lower case; matched as whole words, so "tweet" also matches "tweets")
AREAS: list[tuple[str, str, tuple[str, ...]]] = [
    ("social_media", "Social media & platforms",
     ("social media", "twitter", "tweet", "instagram", "facebook", "tiktok", "youtube", "reddit", "whatsapp",
      "telegram", "weibo", "platform", "influencer")),
    ("politics", "Politics & elections",
     ("election", "electoral", "parliament", "political", "politics", "party", "parties", "vote", "voting",
      "campaign", "government", "policy", "populism", "populist")),
    ("movements", "Social movements & activism",
     ("protest", "protests", "movement", "movements", "activism", "activist", "mobilization", "mobilisation",
      "collective action", "demonstration", "grassroots")),
    ("news", "Journalism & news media",
     ("journalism", "journalist", "journalists", "news", "newspaper", "newspapers", "press", "broadcast",
      "media coverage")),
    ("misinformation", "Misinformation & trust",
     ("misinformation", "disinformation", "fake news", "conspiracy", "trust", "credibility", "propaganda",
      "rumour", "rumor", "fact-checking")),
    ("digital_methods", "Digital methods & data",
     ("computational", "network analysis", "machine learning", "text mining", "big data", "scraping", "dataset",
      "corpus", "topic model", "topic modeling", "digital methods", "sentiment analysis")),
    ("images", "Images & visual communication",
     ("image", "images", "visual", "photograph", "photographs", "video", "videos", "meme", "memes", "visual culture")),
    ("culture", "Culture, identity & everyday life",
     ("identity", "culture", "cultural", "everyday", "gender", "race", "racism", "religion", "youth", "migration",
      "diaspora", "community")),
    ("health_env", "Health & environment",
     ("health", "climate", "environment", "environmental", "pandemic", "covid", "vaccine", "vaccination",
      "emissions", "pollution", "wellbeing", "well-being")),
    ("technology", "Technology, AI & society",
     ("artificial intelligence", "chatbot", "chatbots", "algorithm", "algorithms", "algorithmic", "surveillance",
      "privacy", "automation", "technology", "technologies")),
    (OTHER, "Other", ()),
]

NAMES: dict[str, str] = {area_id: name for area_id, name, _ in AREAS}
IDS: tuple[str, ...] = tuple(area_id for area_id, _, _ in AREAS)
CUES: dict[str, re.Pattern[str]] = {
    area_id: re.compile(r"\b(?:" + "|".join(re.escape(c) for c in cues) + r")\b")
    for area_id, _, cues in AREAS if cues
}
CONFIDENCE = ("high", "medium", "low")
MAX_ALSO = 2


@dataclass
class Placement:
    primary: str
    also: list[str] = field(default_factory=list)
    confidence: str = "low"
    source: str = "rules"


def clean(primary: str | None, also: list[str] | None) -> tuple[str, list[str]]:
    """A valid primary area and up to MAX_ALSO other valid areas, without duplicates."""
    primary = primary if primary in NAMES else OTHER
    extra: list[str] = []
    for area in also or []:
        if area in NAMES and area not in (primary, OTHER) and area not in extra:
            extra.append(area)
    return primary, extra[:MAX_ALSO]


def rules(text: str) -> Placement:
    """Place a paper by counting the cue words of each area in its text (lower case)."""
    counts = {area: len(pattern.findall(text)) for area, pattern in CUES.items()}
    ranked = sorted((a for a in counts if counts[a] > 0), key=lambda a: (-counts[a], a))
    if not ranked:
        return Placement(primary=OTHER, confidence="low", source="rules")
    best = counts[ranked[0]]
    also = [a for a in ranked[1:] if counts[a] >= max(2, best / 2)][:MAX_ALSO]
    confidence = "medium" if best >= 3 else "low"
    primary, also = clean(ranked[0], also)
    return Placement(primary=primary, also=also, confidence=confidence, source="rules")
