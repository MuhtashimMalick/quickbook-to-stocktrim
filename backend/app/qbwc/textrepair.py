"""Repair QuickBooks' numeric character references before the qbXML is parsed.

QuickBooks writes non-ASCII text as numeric references using Windows-1252 code points
(`&#148;` = closing quote). XML maps those to invisible C1 controls (U+0094), so we map
128-159 through cp1252 instead. Invalid XML 1.0 characters (NUL, most C0 controls), as
references or raw, are dropped: they make the parser reject the whole response.

Literal `&#nnnn;` typed into a name arrives escaped (`&amp;#0174;`); it is the real stored
name, so it is left untouched here (cleaned later, when StockTrim descriptions are built).
"""
import re

_REF = re.compile(r"&#(?:x([0-9a-fA-F]+)|([0-9]+));")  # digit runs are unbounded so long ones match (and are dropped)
_RAW_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _fix(m: re.Match) -> str:
    digits, base = (m[1], 16) if m[1] else (m[2], 10)
    if len(digits.lstrip("0")) > 8:  # never int() a huge string (3.11+ 4300-digit limit); invalid XML anyway
        return ""
    n = int(digits, base)
    if n in (9, 10, 13) or 32 <= n < 128 or 160 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF:
        return m[0]
    if 128 <= n <= 159:
        try:
            return f"&#{ord(bytes([n]).decode('cp1252'))};"
        except UnicodeDecodeError:  # 0x81 0x8D 0x8F 0x90 0x9D are undefined in cp1252
            return ""
    return ""  # control codes, surrogates, FFFE/FFFF, > 10FFFF are invalid in XML 1.0


def repair_qbxml(text: str) -> str:
    return _REF.sub(_fix, _RAW_BAD.sub("", text))  # raw first, so "&\x01#148;" becomes a repairable reference
