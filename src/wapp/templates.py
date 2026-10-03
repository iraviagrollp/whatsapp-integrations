"""Approved templates: what each needs filled in, and the filled-in message.

A template's fixed text is what Meta approved and cannot change; what can
change per send is the media in its header and the values in its ``{{1}}``
slots.  The inbox offers a template whenever the 24-hour window to a customer
has closed, since nothing else may be sent then.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_SLOT = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
MEDIA_FORMATS = ("IMAGE", "VIDEO", "DOCUMENT")


class TemplateError(ValueError):
    """The values given do not fit the template."""


def slots(text: Optional[str]) -> List[str]:
    """``{{1}}``, ``{{2}}`` or ``{{name}}`` slots, each once, in order."""
    found: List[str] = []
    for slot in _SLOT.findall(text or ""):
        if slot not in found:
            found.append(slot)
    return sorted(found, key=int) if all(s.isdigit() for s in found) else found


def summary(template: Dict[str, Any]) -> Dict[str, Any]:
    """What the inbox shows of a template, and what it asks for."""
    header_format, header_text, body, footer, buttons = None, "", "", "", []
    for part in template.get("components", []):
        kind = part.get("type")
        if kind == "HEADER":
            header_format = part.get("format")
            header_text = part.get("text", "")
        elif kind == "BODY":
            body = part.get("text", "")
        elif kind == "FOOTER":
            footer = part.get("text", "")
        elif kind == "BUTTONS":
            buttons = [b.get("text", "") for b in part.get("buttons", [])]
    return {
        "name": template["name"],
        "language": template["language"],
        "category": (template.get("category") or "").lower(),
        "header_format": header_format,
        "header_text": header_text,
        "header_slots": slots(header_text) if header_format == "TEXT" else [],
        "body": body,
        "body_slots": slots(body),
        "footer": footer,
        "buttons": buttons,
        "sendable": not any("{{" in (b.get("url") or "")
                            for p in template.get("components", []) for b in p.get("buttons", [])),
    }


def _text_parameters(names: List[str], values: List[str], named: bool) -> List[Dict[str, Any]]:
    parameters = []
    for name, value in zip(names, values):
        parameter: Dict[str, Any] = {"type": "text", "text": value}
        if named:
            parameter["parameter_name"] = name
        parameters.append(parameter)
    return parameters


def build(
    template: Dict[str, Any],
    *,
    header_values: List[str],
    body_values: List[str],
    media_id: Optional[str] = None,
    filename: str = "",
) -> Dict[str, Any]:
    """The ``template`` content of a message, checked against what the template needs."""
    info = summary(template)
    if not info["sendable"]:
        raise TemplateError("This template has a link button with a slot, which the inbox cannot fill.")
    named = template.get("parameter_format") == "NAMED"
    components: List[Dict[str, Any]] = []

    if info["header_format"] in MEDIA_FORMATS:
        kind = info["header_format"].lower()
        if not media_id:
            raise TemplateError(f"This template's header is {'an' if kind == 'image' else 'a'} {kind}: attach the file.")
        media: Dict[str, Any] = {"id": media_id}
        if kind == "document" and filename:
            media["filename"] = filename
        components.append({"type": "header", "parameters": [{"type": kind, kind: media}]})
    elif info["header_slots"]:
        if len(header_values) != len(info["header_slots"]) or not all(v.strip() for v in header_values):
            raise TemplateError("Fill in the header's blank.")
        components.append({"type": "header",
                           "parameters": _text_parameters(info["header_slots"], header_values, named)})

    if len(body_values) != len(info["body_slots"]) or not all(v.strip() for v in body_values):
        raise TemplateError(f"Fill in all {len(info['body_slots'])} blank(s) in the message.")
    if info["body_slots"]:
        components.append({"type": "body", "parameters": _text_parameters(info["body_slots"], body_values, named)})

    content: Dict[str, Any] = {"name": template["name"], "language": {"code": template["language"]}}
    if components:
        content["components"] = components
    return {"type": "template", "template": content}


def preview(template: Dict[str, Any], header_values: List[str], body_values: List[str]) -> str:
    """The template's text with the blanks filled, for the chat to show what was sent."""
    info = summary(template)
    text = info["body"]
    for slot, value in zip(info["body_slots"], body_values):
        text = re.sub(r"\{\{\s*" + re.escape(slot) + r"\s*\}\}", value, text)
    header = info["header_text"]
    for slot, value in zip(info["header_slots"], header_values):
        header = re.sub(r"\{\{\s*" + re.escape(slot) + r"\s*\}\}", value, header)
    return f"*{header}*\n{text}" if header else text
