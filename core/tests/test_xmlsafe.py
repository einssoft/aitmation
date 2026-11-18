"""XML from outside is parsed without document type or entity declarations."""

import xml.etree.ElementTree as ET

import pytest

from aitmation_core import xmlsafe
from aitmation_core.bpmn import BpmnError, parse_bpmn

BOMB = (
    '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">'
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><d>&b;</d>'
)


def test_plain_documents_parse():
    assert xmlsafe.fromstring("<a><b>x</b></a>").find("b").text == "x"
    assert xmlsafe.fromstring(b"<a><b>x</b></a>").find("b").text == "x"


@pytest.mark.parametrize("document", [BOMB, BOMB.encode(), BOMB.encode("utf-16")])
def test_declarations_are_refused(document):
    with pytest.raises(ET.ParseError):
        xmlsafe.fromstring(document)


def test_a_process_file_with_a_doctype_does_not_load():
    with pytest.raises(BpmnError):
        parse_bpmn(BOMB, source="bomb.bpmn")
