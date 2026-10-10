"""Rewrite scientific text so a TTS engine reads it naturally.

normalize("Accuracy rose by 5 % (p < 0.05) [12], see Fig. 3.")
    -> "Accuracy rose by 5 percent (p less than 0.05), see Figure 3."
"""
from __future__ import annotations

import re
from pathlib import Path

LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
INVISIBLE = re.compile(r"[­​‌‍⁠﻿]")

URL = re.compile(r"\b(?:https?://|www\.)\S+|\bdoi:\s*\S+|\b10\.\d{4,9}/\S+", re.I)
NUMERIC_CITATION = re.compile(r"\s?\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]")
_NAME = r"[A-Z][A-Za-z'’\-]+(?:\s(?:et\sal\.|and|&)\s?(?:[A-Z][A-Za-z'’\-]+)?)?"
_YEAR = r"(?:19|20)\d{2}[a-z]?"
AUTHOR_YEAR_CITATION = re.compile(
    rf"\s?\((?:e\.g\.,?\s|see\s)?{_NAME},?\s{_YEAR}(?:,\s{_YEAR})*(?:;\s?{_NAME},?\s{_YEAR}(?:,\s{_YEAR})*)*\)"
)
NARRATIVE_YEAR = re.compile(rf"(et al\.|\b[A-Z][a-z]+)\s\(({_YEAR})\)")

ABBREVIATIONS = [
    (r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\bcf\.", "compare"),
    (r"\bvs\.?(?=\s)", "versus"), (r"\betc\.", "et cetera"), (r"\bapprox\.", "approximately"),
    (r"\bresp\.", "respectively"), (r"\bw\.r\.t\.", "with respect to"), (r"\bet al\.", "and colleagues"),
    (r"\bFigs\.", "Figures"), (r"\bFig\.", "Figure"), (r"\bEqs\.", "Equations"), (r"\bEq\.", "Equation"),
    (r"\bTab\.", "Table"), (r"\bSect?\.", "Section"), (r"\bNo\.(?=\s?\d)", "number"),
    (r"\bca\.(?=\s?\d)", "about"), (r"\bRefs?\.", "reference"), (r"\bSuppl\.", "supplementary"),
]

GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ζ": "zeta", "η": "eta",
    "θ": "theta", "κ": "kappa", "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "π": "pi", "ρ": "rho",
    "σ": "sigma", "τ": "tau", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega", "Γ": "gamma",
    "Δ": "delta", "Θ": "theta", "Λ": "lambda", "Π": "pi", "Σ": "sigma", "Φ": "phi", "Ψ": "psi", "Ω": "omega",
}
SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")

UNITS = {
    "ms": "millisecond", "µs": "microsecond", "μs": "microsecond", "ns": "nanosecond",
    "min": "minute", "Hz": "hertz", "kHz": "kilohertz", "MHz": "megahertz", "GHz": "gigahertz",
    "nm": "nanometer", "µm": "micrometer", "μm": "micrometer", "mm": "millimeter", "cm": "centimeter",
    "km": "kilometer", "mg": "milligram", "µg": "microgram", "μg": "microgram", "kg": "kilogram",
    "mL": "milliliter", "ml": "milliliter", "µL": "microliter", "μL": "microliter",
    "mM": "millimolar", "µM": "micromolar", "μM": "micromolar", "nM": "nanomolar",
    "kDa": "kilodalton", "Da": "dalton", "kB": "kilobyte", "MB": "megabyte", "GB": "gigabyte",
    "TB": "terabyte", "kW": "kilowatt", "mV": "millivolt", "kV": "kilovolt", "°C": "degrees Celsius",
    "°F": "degrees Fahrenheit",
}
_UNIT_RE = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(" + "|".join(sorted(map(re.escape, UNITS), key=len, reverse=True)) + r")(?![\w])"
)

SYMBOLS = [
    (r"\s?≤\s?", " less than or equal to "), (r"\s?≥\s?", " greater than or equal to "),
    (r"\s?≠\s?", " not equal to "), (r"\s?≈\s?", " approximately "), (r"\s?±\s?", " plus or minus "),
    (r"\s?→\s?", " leads to "), (r"\s?∝\s?", " proportional to "), (r"\s?∼\s?(?=\d)", " about "),
    (r"(?<![\w~])~\s?(?=\d)", "about "), (r"\s<\s?(?=[\d.\-])", " less than "),
    (r"\s>\s?(?=[\d.\-])", " greater than "), (r"\s=\s?", " equals "), (r"(?<=\w)=(?=\w)", " equals "),
    (r"‰", " per mille"), (r"\s?%", " percent"), (r"°(?![CF])", " degrees"),
]


class Normalizer:
    def __init__(self, lexicon: dict[str, str] | None = None, citations: str = "remove"):
        self.citations = citations
        self.lexicon = [
            (re.compile(rf"(?<![\w-]){re.escape(k)}(?![\w-])"), v)
            for k, v in sorted((lexicon or {}).items(), key=lambda kv: len(kv[0]), reverse=True)
        ]

    @classmethod
    def from_file(cls, path: Path, citations: str = "remove") -> "Normalizer":
        lexicon: dict[str, str] = {}
        if path.is_file():
            import yaml

            data = yaml.safe_load(path.read_text()) or {}
            lexicon = {str(k): str(v) for k, v in (data.get("words") or {}).items()}
        return cls(lexicon, citations)

    def __call__(self, text: str) -> str:
        t = INVISIBLE.sub("", text).replace(" ", " ").replace(" ", " ")
        for lig, rep in LIGATURES.items():
            t = t.replace(lig, rep)
        t = URL.sub("", t)

        if self.citations == "remove":
            t = NUMERIC_CITATION.sub("", t)
            t = AUTHOR_YEAR_CITATION.sub("", t)
            t = NARRATIVE_YEAR.sub(r"\1", t)

        for pattern, word in self.lexicon:
            t = pattern.sub(word, t)
        for pattern, rep in ABBREVIATIONS:
            t = re.sub(pattern, rep, t)

        t = self._scientific_numbers(t)
        t = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", r"\1/\2/\3", t)            # keep ISO dates intact
        t = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s?[–—-]\s?(\d+(?:\.\d+)?)(?![\w/])", r"\1 to \2", t)
        t = _UNIT_RE.sub(lambda m: f"{m.group(1)} {self._unit(m.group(1), m.group(2))}", t)
        for pattern, rep in SYMBOLS:
            t = re.sub(pattern, rep, t)
        for letter, name in GREEK.items():
            t = t.replace(letter, f" {name} ")
        t = re.sub(r"\$[^$]{1,200}\$", " a formula ", t)

        t = re.sub(r"\(\s*[,;]?\s*\)|\[\s*\]", "", t)
        t = re.sub(r"[†‡§¶]", "", t)                                          # footnote marks
        t = re.sub(r"(?<!\S)[\[\]*]+(?!\S)", "", t)                            # stray brackets, asterisks
        t = re.sub(r"\s+([,.;:!?)])", r"\1", t)
        t = re.sub(r"([(])\s+", r"\1", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        return t

    @staticmethod
    def _unit(number: str, unit: str) -> str:
        name = UNITS[unit]
        if name.startswith("degrees") or name.endswith("hertz") or number in ("1", "1.0"):
            return name
        return name + "s"

    @staticmethod
    def _scientific_numbers(t: str) -> str:
        def power(m: re.Match) -> str:
            exp = m.group("exp").translate(SUPERSCRIPT).replace("−", "-")
            sign = "minus " if exp.startswith("-") else ""
            return f"{m.group('base')} to the power of {sign}{exp.lstrip('+-')}"

        t = re.sub(r"(\d)\s?[×x]\s?(?=10)", r"\1 times ", t)
        t = re.sub(r"(?P<base>10)(?:\^|\*\*)(?P<exp>[-−+]?\d+)", power, t)
        t = re.sub(r"(?P<base>10)(?P<exp>[⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)", power, t)
        t = re.sub(r"(\d(?:\.\d+)?)[eE]([-+]?\d+)\b",
                   lambda m: f"{m.group(1)} times 10 to the power of "
                             f"{'minus ' if m.group(2).startswith('-') else ''}{m.group(2).lstrip('+-')}", t)
        t = re.sub(r"(?<=\w)²", " squared", t)
        t = re.sub(r"(?<=\w)³", " cubed", t)
        t = re.sub(r"\s?×\s?", " times ", t)
        return t
