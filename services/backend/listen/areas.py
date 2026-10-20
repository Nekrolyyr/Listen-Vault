"""Research areas: a fixed list of broad fields that every paper is placed in (see worker/classify.py).

The list is the 26 fields of the OpenAlex taxonomy (the same fields as Scopus's subject areas), so that any paper has
a field to go in. It lives in code so that the LLM can only choose from it and the screens can rely on the ids. The
map shows only the areas that have papers in the library. A paper has one primary area and up to two more; "other" is
used only when nothing else fits. Rules are the fallback when the LLM is not available: they count the cue words of
each area in the title, abstract, keywords and sections.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OTHER = "other"

# id, name, cue words (lower case; matched as whole words, so "tweet" also matches "tweets")
AREAS: list[tuple[str, str, tuple[str, ...]]] = [
    ("agri_bio", "Agricultural and Biological Sciences",
     ("agriculture", "agricultural", "crop", "crops", "plant", "plants", "ecology", "ecological", "animal", "animals",
      "species", "forestry", "fisheries", "soil", "livestock")),
    ("arts", "Arts and Humanities",
     ("history", "historical", "philosophy", "philosophical", "literature", "linguistics", "language", "languages",
      "art", "music", "theology", "archaeology", "classics", "humanities", "poetry", "rhetoric", "heritage")),
    ("biochem", "Biochemistry, Genetics and Molecular Biology",
     ("protein", "proteins", "gene", "genes", "genetic", "genetics", "enzyme", "enzymes", "dna", "rna", "molecular",
      "biochemistry", "genome", "genomic", "genomics")),
    ("business", "Business, Management and Accounting",
     ("business", "management", "firm", "firms", "organization", "organizations", "organisation", "organisations",
      "marketing", "entrepreneurship", "corporate", "accounting", "employees", "leadership", "brand", "brands")),
    ("chem_eng", "Chemical Engineering",
     ("chemical engineering", "reactor", "reactors", "catalysis", "distillation", "process engineering")),
    ("chemistry", "Chemistry",
     ("chemistry", "chemical", "chemicals", "compound", "compounds", "molecule", "molecules", "reaction", "reactions",
      "synthesis", "spectroscopy")),
    ("computer_science", "Computer Science",
     ("algorithm", "algorithms", "algorithmic", "software", "computing", "computational", "machine learning",
      "neural network", "neural networks", "deep learning", "dataset", "datasets", "network analysis",
      "natural language processing", "database", "programming", "artificial intelligence", "chatbot", "chatbots",
      "sentiment analysis", "text mining", "topic model", "topic modeling", "big data", "scraping", "corpus")),
    ("decision", "Decision Sciences",
     ("decision making", "decision-making", "operations research", "optimization", "optimisation", "forecasting",
      "simulation", "game theory", "decision analysis")),
    ("earth", "Earth and Planetary Sciences",
     ("geology", "geological", "earthquake", "earthquakes", "ocean", "oceans", "atmosphere", "volcano", "planetary",
      "geophysics", "seismic")),
    ("economics", "Economics, Econometrics and Finance",
     ("economics", "economic", "economy", "finance", "financial", "market", "markets", "inflation", "trade",
      "econometric", "wages", "income", "banking")),
    ("energy", "Energy",
     ("energy", "renewable", "renewables", "solar", "fossil fuel", "fossil fuels", "electricity", "battery",
      "batteries", "hydrogen")),
    ("engineering", "Engineering",
     ("engineering", "mechanical", "electrical", "robotics", "robot", "robots", "sensor", "sensors", "control system",
      "manufacturing", "structural", "automation")),
    ("environment", "Environmental Science",
     ("environment", "environmental", "climate", "climate change", "pollution", "emissions", "carbon",
      "sustainability", "sustainable", "biodiversity", "ecosystem", "ecosystems")),
    ("immunology", "Immunology and Microbiology",
     ("immunology", "immune", "vaccine", "vaccines", "vaccination", "bacteria", "bacterial", "virus", "viral",
      "microbiology", "pathogen", "pathogens", "infection", "infections", "covid", "pandemic")),
    ("materials", "Materials Science",
     ("materials", "nanomaterials", "polymer", "polymers", "alloy", "alloys", "nanoparticles", "ceramic", "composite",
      "composites", "graphene")),
    ("maths", "Mathematics",
     ("mathematics", "mathematical", "theorem", "theorems", "proof", "proofs", "statistics", "statistical",
      "probability", "regression", "bayesian", "stochastic", "topology", "algebra")),
    ("medicine", "Medicine",
     ("medicine", "medical", "clinical", "patient", "patients", "disease", "diseases", "hospital", "hospitals",
      "diagnosis", "therapy", "cancer", "surgery", "epidemiology", "mortality")),
    ("neuroscience", "Neuroscience",
     ("neuroscience", "brain", "neurons", "cortex", "fmri", "eeg", "neurological")),
    ("nursing", "Nursing",
     ("nursing", "nurse", "nurses", "midwifery", "caregiving", "caregivers")),
    ("pharmacology", "Pharmacology, Toxicology and Pharmaceutics",
     ("pharmacology", "pharmacological", "drug", "drugs", "pharmaceutical", "pharmaceuticals", "toxicology",
      "toxicity", "dosage", "medication", "medications")),
    ("physics", "Physics and Astronomy",
     ("physics", "quantum", "particle", "particles", "astronomy", "astrophysics", "galaxy", "galaxies", "relativity",
      "optics", "photon", "photons")),
    ("psychology", "Psychology",
     ("psychology", "psychological", "psychologists", "cognitive", "behaviour", "behavior", "emotion", "emotions",
      "personality", "anxiety", "depression", "well-being", "wellbeing", "attitudes", "mental health")),
    ("social", "Social Sciences",
     ("social", "sociology", "sociological", "society", "politics", "political", "election", "elections", "electoral",
      "parliament", "policy", "governance", "inequality", "gender", "migration", "anthropology", "geography",
      "demography", "criminology", "journalism", "journalist", "journalists", "news", "newspaper", "newspapers",
      "misinformation", "disinformation", "propaganda", "protest", "protests", "activism", "movement", "movements",
      "community", "culture", "cultural", "identity", "twitter", "tweet", "tweets", "facebook", "instagram",
      "tiktok", "youtube", "reddit", "platform", "platforms", "influencer")),
    ("veterinary", "Veterinary",
     ("veterinary", "veterinarian", "veterinarians", "dogs", "cats", "pets", "animal health")),
    ("dentistry", "Dentistry",
     ("dentistry", "dental", "dentist", "dentists", "oral health", "teeth", "periodontal")),
    ("health_prof", "Health Professions",
     ("health professions", "physiotherapy", "occupational therapy", "rehabilitation", "nutrition", "dietetics",
      "health services", "health care", "healthcare", "public health")),
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
