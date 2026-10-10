import pytest

from listen.worker.normalize import Normalizer
from listen.worker.sentences import split

norm = Normalizer({"SARS-CoV-2": "sars cov two", "CRISPR": "crisper"})


@pytest.mark.parametrize(
    "text, spoken",
    [
        ("Accuracy rose by 5 % (p < 0.05) [12], see Fig. 3.",
         "Accuracy rose by 5 percent (p less than 0.05), see Figure 3."),
        ("as shown previously [3–5, 9].", "as shown previously."),
        ("Attention helps (Vaswani et al., 2017; Devlin et al., 2019).", "Attention helps."),
        ("This was observed (Smith & Jones, 2020a, 2021).", "This was observed."),
        ("Smith et al. (2020) proposed it.", "Smith and colleagues proposed it."),
        ("e.g. transformers, i.e. attention models, vs. RNNs", "for example transformers, that is attention models, versus RNNs"),
        ("Samples were heated to 37 °C for 15 min.", "Samples were heated to 37 degrees Celsius for 15 minutes."),
        ("Latency was 1 ms and 250ms.", "Latency was 1 millisecond and 250 milliseconds."),
        ("a 5 µm pore at 2.4 GHz", "a 5 micrometers pore at 2.4 gigahertz"),
        ("We used 10–20 epochs.", "We used 10 to 20 epochs."),
        ("in 2019-2021", "in 2019 to 2021"),
        ("released 2021-03-04", "released 2021/03/04"),
        ("a rate of 3 × 10^5 per s", "a rate of 3 times 10 to the power of 5 per s"),
        ("lr = 1e-4", "lr equals 1 times 10 to the power of minus 4"),
        ("about 10⁻³ of cases", "about 10 to the power of minus 3 of cases"),
        ("the area is 4 m²", "the area is 4 m squared"),
        ("α = 0.5 and β ≤ 2", "alpha equals 0.5 and beta less than or equal to 2"),
        ("mean ± SD", "mean plus or minus SD"),
        ("~20 participants", "about 20 participants"),
        ("SARS-CoV-2 and CRISPR", "sars cov two and crisper"),
        ("See https://example.org/x and doi:10.1000/xyz123 now.", "See and now."),
        ("eﬃcient ﬁne-tuning", "efficient fine-tuning"),
        ("COVID-19 cases", "COVID-19 cases"),
        ("the function $f(x)$ is smooth", "the function a formula is smooth"),
    ],
)
def test_normalize(text, spoken):
    assert norm(text) == spoken


def test_citations_can_be_spoken():
    speak = Normalizer(citations="speak")
    assert speak("Smith et al. (2020) proposed it [4].") == "Smith and colleagues (2020) proposed it [4]."


@pytest.mark.parametrize(
    "text, expected",
    [
        ("We tested it. It worked.", ["We tested it.", "It worked."]),
        ("See Fig. 2 for details. Results follow.", ["See Fig. 2 for details.", "Results follow."]),
        ("As Smith et al. (2020) showed, it holds.", ["As Smith et al. (2020) showed, it holds."]),
        ("Written by J. Smith in 2020. Next one.", ["Written by J. Smith in 2020.", "Next one."]),
        ("Values were small, e.g. 0.1. The rest is large.", ["Values were small, e.g. 0.1.", "The rest is large."]),
        ("It rose 3.5 percent. 2024 was different.", ["It rose 3.5 percent.", "2024 was different."]),
        ('He said "stop." Then left.', ['He said "stop."', "Then left."]),
        ("", []),
    ],
)
def test_split(text, expected):
    assert split(text) == expected


def test_split_long_sentence():
    long = "word " * 40 + "; " + "more " * 40 + "end."
    parts = split(long)
    assert len(parts) == 2 and all(len(p) <= 350 for p in parts)
