from lxml import etree

from listen.worker import boxes, grobid
from listen.worker.docmodel import Paragraph

TEI_NS = "http://www.tei-c.org/ns/1.0"


def test_line_boxes_from_tei_coords():
    p = etree.fromstring(
        f'<p xmlns="{TEI_NS}" coords="2,70.0,100.0,400.0,12.0;2,70.0,115.0,250.5,12.0;bad,1,2">x</p>'
    )
    assert grobid._boxes(p) == [[2, 70.0, 100.0, 400.0, 12.0], [2, 70.0, 115.0, 250.5, 12.0]]
    # coordinates may sit on a descendant (GROBID puts them on <s> elements)
    outer = etree.fromstring(f'<p xmlns="{TEI_NS}"><s coords="3,1,2,3,4">x</s></p>')
    assert grobid._boxes(outer) == [[3, 1, 2, 3, 4]]
    assert grobid._boxes(etree.fromstring(f'<p xmlns="{TEI_NS}">no coords</p>')) == []


def test_sentence_spans_lines_by_width():
    lines = [[1, 50.0, 100.0, 200.0, 10.0], [1, 50.0, 112.0, 100.0, 10.0]]  # 300 pt of text in total
    text = "abcdefghij" * 30  # 300 characters: 1 point per character
    # the first sentence covers the first line completely and 40 points of the second, so it is split
    rects = boxes.sentence_rects(lines, text, 0, 240)
    assert rects == [[1, 50.0, 100.0, 200.0, 10.0], [1, 50.0, 112.0, 40.0, 10.0]]
    # a sentence inside the second line keeps its horizontal position there
    assert boxes.sentence_rects(lines, text, 250, 300) == [[1, 50.0 + 50.0, 112.0, 50.0, 10.0]]
    assert boxes.sentence_rects([], text, 0, 10) == []
    assert boxes.sentence_rects(lines, "", 0, 0) == []


def test_paragraph_rects_cover_whole_text():
    lines = [[4, 10.0, 20.0, 80.0, 9.0], [4, 10.0, 30.0, 60.0, 9.0]]
    assert boxes.paragraph_rects(lines, "  two   words ") == [[4, 10.0, 20.0, 80.0, 9.0], [4, 10.0, 30.0, 60.0, 9.0]]


def test_paragraph_model_keeps_boxes_when_loaded():
    p = Paragraph(text="t", page=1, boxes=[[1, 2.0, 3.0, 4.0, 5.0]])
    assert p.boxes == [[1, 2.0, 3.0, 4.0, 5.0]]
    assert Paragraph(text="old").boxes == []  # structure files written before this change still load
