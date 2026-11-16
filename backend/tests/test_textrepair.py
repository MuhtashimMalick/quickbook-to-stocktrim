import pytest

from app.qbwc.textrepair import repair_qbxml


@pytest.mark.parametrize("src,want", [
    ("&#148;", "&#8221;"), ("&#x94;", "&#8221;"), ("&#00150;", "&#8211;"),
    ("&#153;", "&#8482;"), ("&#146;", "&#8217;"), ("&#128;", "&#8364;"),
    ("&#129;&#x8D;&#143;&#144;&#x9d;", ""),  # undefined in cp1252: dropped
    ("&#225;&#188;&#174;&#160;", "&#225;&#188;&#174;&#160;"),  # >= 160 untouched
    ("&#0;&#8;&#11;&#12;&#14;&#31;&#x0;", ""),  # invalid in XML 1.0
    ("a\x00b\x08c\x0bd\x0ce\x0ef\x1fg", "abcdefg"),  # raw invalid controls
    ("t\tn\nr\r &amp; &lt; &#9;&#10;&#13;&#65;", "t\tn\nr\r &amp; &lt; &#9;&#10;&#13;&#65;"),
    ("&amp;#0174; &amp;#148;", "&amp;#0174; &amp;#148;"),  # literal text typed into a name: data, not a reference
    ("&#xD800;&#xDFFF;&#xFFFE;&#xFFFF;&#x110000;", ""),  # surrogates, non-characters, out of range
    ("&#" + "1" * 5000 + ";&#x" + "1" * 5000 + ";", ""),  # must not hit int()'s digit limit
    ("&\x01#148;", "&#8221;"),  # raw junk stripped first
])
def test_repair(src, want):
    assert repair_qbxml(src) == want
    assert repair_qbxml(repair_qbxml(src)) == want  # idempotent
