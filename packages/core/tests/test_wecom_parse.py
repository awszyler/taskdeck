from __future__ import annotations

from taskdeck_core.im.wecom.parse import parse_inner_message, parse_outer_encrypt


def test_parse_outer_encrypt_strips_xml_wrapper():
    body = """<xml>
<ToUserName><![CDATA[corp]]></ToUserName>
<Encrypt><![CDATA[abc123ciphertext]]></Encrypt>
</xml>"""
    assert parse_outer_encrypt(body) == "abc123ciphertext"


def test_parse_inner_message_extracts_fields():
    xml = """<xml>
<ToUserName>corp</ToUserName>
<FromUserName>UserA</FromUserName>
<CreateTime>1700000000</CreateTime>
<MsgType>text</MsgType>
<Content>hello world</Content>
<MsgId>12345</MsgId>
<AgentID>1000</AgentID>
</xml>"""
    m = parse_inner_message(xml)
    assert m["FromUserName"] == "UserA"
    assert m["Content"] == "hello world"
    assert m["MsgType"] == "text"
