from __future__ import annotations
import xml.etree.ElementTree as ET

from dataclasses import dataclass, asdict, field
from datetime import datetime
from json import dumps

import cmd_udp
from prot_udp import prot_udp
from prot_udp import tests as prot_udp_tests


def _elem_to_dict(el: ET.Element) -> dict:
    """xmltodict-compatible shape: `@attr` keys, repeated tags as lists,
    single child tag as plain dict (matches v720_ap.filename_list)."""
    d = {f'@{k}': v for k, v in el.attrib.items()}
    children = list(el)
    if children:
        by_tag: dict[str, list] = {}
        for ch in children:
            by_tag.setdefault(ch.tag, []).append(_elem_to_dict(ch))
        for tag, lst in by_tag.items():
            d[tag] = lst[0] if len(lst) == 1 else lst
    else:
        text = (el.text or '').strip()
        if text:
            d['#text'] = text
    return d

@dataclass
class prot_xml_udp(prot_udp):
    # public
    xml: dict = field(default_factory=dict)

    def req(self) -> bytes:
        raise NotImplementedError('Camera doesn\'t support XML requests')

    @staticmethod
    def resp(income: bytes) -> prot_xml_udp:
        r = prot_udp.resp(income)
        if r is not None and r.cmd == cmd_udp.P2P_UDP_CMD_XML:
            try:
                root = ET.fromstring(r.payload.decode('ascii'))
                return prot_xml_udp(**asdict(r),
                                    xml={root.tag: _elem_to_dict(root)})
            except (UnicodeDecodeError, ET.ParseError):
                print(f'---Exception with: {r.payload}')
        return None

    def __str__(self) -> str:
        return dumps(self.xml, default=self.__dumps_bytes__, indent=4)

    def __repr__(self) -> str:
        return f'XML(as dict): {self.xml}'


def tests():
    prot_udp_tests()
    xml = '''<?xml version="1.0" encoding="utf-8"?>
<video>
    <catalogue date="20230124" hNumb="9">
        <option hours="2" minBin="864409584620273472"/>
        <option hours="3" minBin="1152912708513561599"/>
        <option hours="4" minBin="1152780767118458879"/>
        <option hours="5" minBin="1150669704792637439"/>
        <option hours="6" minBin="1116892707579494399"/>
        <option hours="7" minBin="1152921504472629247"/>
        <option hours="8" minBin="1152921500311879678"/>
        <option hours="9" minBin="1152921435887370223"/>
        <option hours="10" minBin="9005000231485183"/>
    </catalogue>
</video>
'''

    print('----------- ', prot_xml_udp.__name__, 'Tests ---------------')
    
    income = bytearray([0, 0, 0, 0, cmd_udp.P2P_UDP_CMD_XML, 0, 0,
                   0, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0, 0, 0, 0])
    income[0] = len(xml) & 0xff
    income[1] = len(xml) >> 8
    income.extend(xml.encode('ascii'))

    p = prot_xml_udp.resp(income)
    print('0. ', income, ' -> ', p)
    assert(p.xml is not None)
    assert(int(p.xml['video']['catalogue']['option'][4]['@hours']) == 6)
    assert(int(p.xml['video']['catalogue']['option'][4]['@minBin']) == 1116892707579494399)
    
    print('0. PASS')

    income = bytearray([0, 0, 0, 0, cmd_udp.P2P_UDP_CMD_JSON, 0, 0,
                   0, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0, 0, 0, 0])
    income[0] = len(xml) & 0xff
    income[1] = len(xml) >> 8
    income.extend(xml.encode('ascii'))
    p = prot_xml_udp.resp(income)
    print('1. ', income, ' -> ', p)
    assert(p is None)
    print('1. PASS')

if __name__ == '__main__':
    tests()
