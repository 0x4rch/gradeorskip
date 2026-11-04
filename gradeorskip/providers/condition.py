"""Vision-LLM condition assessment behind a small provider interface.

One call per card side, on a perspective-corrected JPEG downscaled to
MAX_SIDE px on the long edge. Responses are cached on disk by
(provider, model, prompt version, image hash), so re-running a check is free.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

from ..config import DATA_DIR

PROMPT_VERSION = "v7b-phone-frames"  # v6 + phone-camera frames: specks at corners/edges are not whitening; tears always count
MAX_SIDE = 1024
CACHE_DIR = DATA_DIR / "cache" / "llm"

# USD per 1M tokens (input, output).
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
    # OpenAI prices from third-party aggregators (2026-10-28), not openai.com; verify.
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}

AREA_SCHEMA = {
    "type": "object",
    "properties": {
        "area": {"type": "string", "enum": ["corners", "edges", "surface"]},
        "severity": {"type": "string", "enum": ["none", "minor", "moderate", "major"]},
        "confidence": {"type": "number", "description": "0-1: how sure you are given the photo quality"},
        "notes": {"type": "string"},
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "x": {"type": "number"}, "y": {"type": "number"},
                    "w": {"type": "number"}, "h": {"type": "number"},
                },
                "required": ["label", "x", "y", "w", "h"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["area", "severity", "confidence", "notes", "locations"],
    "additionalProperties": False,
}

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "name_printed": {"type": "string", "description": "card name exactly as printed, in its own language"},
        "name_en": {"type": "string", "description": "English name of the card, e.g. 'Charizard ex'"},
        "number": {"type": "string", "description": "collector number as printed, e.g. '4/102', '199/165', 'TG23/TG30', 'No.006'; for promos the digits after the set code, e.g. 'SVP EN 085' -> '085', 'MEP EN 024' -> '024'"},
        "set_name": {"type": "string", "description": "set name if you can tell from the symbol or text (promo code like SVP/MEP/SWSH means 'Promo'), else ''"},
        "language": {"type": "string", "description": "ISO code of the printed language, e.g. 'en', 'ja'"},
        "first_edition": {"type": "boolean", "description": "true only if a '1st Edition' stamp is visible"},
        "stamp": {"type": "string", "description": "any stamp or special mark added to the normal card design, in plain words, e.g. 'Trick or Trade pumpkin', 'Prerelease', 'Staff', 'Pokemon Center', 'Worlds', 'League'. Empty string if none. Not the 1st Edition stamp, set symbol, rarity mark or holo pattern"},
        "confidence": {"type": "number", "description": "0-1: how sure you are of name AND number"},
    },
    "required": ["name_printed", "name_en", "number", "set_name", "language", "first_edition", "stamp", "confidence"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "card": CARD_SCHEMA,
        "is_trading_card": {"type": "boolean"},
        "in_holder": {"type": "boolean", "description": "true if the card is inside a sleeve, toploader, case or slab"},
        "inspection": {"type": "string", "enum": ["good", "limited", "poor"],
                       "description": "how well this photo let you inspect fine flaws: good = small whitening and fine scratches would be visible; limited = softness, glare or lighting could hide them; poor = you can only see obvious damage"},
        "image_usable": {"type": "boolean"},
        "image_issues": {"type": "array", "items": {"type": "string"}},
        "areas": {"type": "array", "items": AREA_SCHEMA},
        "summary": {"type": "string"},
    },
    "required": ["card", "in_holder", "inspection", "is_trading_card", "image_usable", "image_issues", "areas", "summary"],
    "additionalProperties": False,
}

SYSTEM = """You inspect a photo of ONE side of a raw trading card for physical damage, the way a strict but fair grader would.
The image has usually been cropped and straightened already. Centering is measured separately; ignore it.
The photos are frames from a phone camera, about 1,200 px across the card. At that size, light specks and a thin light rim along corners and edges are nearly always foil or silver-border sparkle, the card's cut edge catching light, sharpening halos or sensor noise. The person holding the card checks fine whitening better by eye, so only report what is unmistakable in a photo.
That restraint is for specks and fine whitening only. A tear, a nick or chip missing from the edge, a crease, a bend or a dent is always reportable, even in a soft or glary photo.

Return exactly three entries in `areas`: corners, edges, surface.
- corners: whitening (white specks where the coloured border meets the corner), fraying, dings, bent or folded corners.
- edges: whitening or chipping along the four edges, nicks, rough or wavy cuts.
- surface: scratches, scuffs, print lines, dents or indentations, creases, bends, stains, tears.

Set `in_holder` to true if the card is inside any plastic: a penny sleeve, toploader, magnetic case or graded slab.
Set `inspection` honestly: finding nothing in a soft or glary photo is not the same as the card being clean.

Also fill `card` to identify the card (front only; on a back, use empty strings and confidence 0).
Read the name and the collector number from the card itself (the number is usually printed at the bottom). Extra images may follow the main one, each introduced by a caption:
- "Close-up of the bottom strip": use it only to read the collector number and set code, not for condition.
- "Close-up of the four corners": the card's corners at full resolution, in a 2x2 grid. Judge `corners` from it: report whitening, fraying or a ding only if you can see it there, and place its location box on the MAIN image. If the corners look clean in the close-up, corners are "none", however soft the main image is.
- "Tilted shot": the same side tilted under a light. Use it only to find surface defects that show at an angle (scratches, scuffs, print lines, dents, indentations) and report them under `surface`, with locations estimated on the MAIN image. Reflections, glare and rainbow holo sheen in it are expected, not defects; don't judge corners, edges or centering from it. Don't guess a number you can't read: leave it empty and lower the confidence.

Severity scale:
- none: you cannot point to a specific physical flaw in this area. THIS IS THE DEFAULT.
- minor: one small flaw you can point to (a solid white patch on one corner, one faint print line, one light scratch). Not a sparkle, a light rim or a speck.
- moderate: damage a buyer would notice at arm's length: a chipped or frayed corner, a run of solid whitening or chipping along an edge, a clear scratch across the art. Never for specks or sparkle, however many.
- major: a crease, bend, fold, dent, tear, heavy whitening, or water damage.

What is NOT a defect (never report these):
- Rounded corners. All modern trading cards have rounded corners by design; only call corners if there is whitening, fraying or a bend.
- Image artefacts: noise, grain, JPEG compression, blur, moire, low resolution, colour casts.
- Normal printing: holofoil sparkle and texture, textured/etched full-art surfaces, card art, borders that look slightly uneven in width.
- Things that aren't the card: glare, reflections, shadows, dust or hair lying on top, fingers, sleeves, toploaders, slab plastic and scratches on that plastic.
- Anything outside the card.

Rules:
- Every severity other than "none" MUST include at least one location box around the flaw (normalised 0-1 coordinates, origin top-left of the image). If you can't place it, it's "none".
- Bends, creases and dents often show only as a line or ripple in a reflection; call them major when you see that. But on holofoil, textured and full-art cards, lines, streaks and ripples inside the foil pattern are printing: call a crease only when one line runs straight across the art and continues into the border, or bends a reflection across the card.
- `confidence` is how sure you are that your severity call is right. If the photo is too blurry, small or glary to judge an area, say "none" with low confidence and explain in image_issues; do not guess at damage.
- Notes: short and specific, e.g. "white speck at top-left corner", "crease running diagonally across lower right"."""

USER = "Assess the {side} of this card. Respond with JSON only."


class ConditionError(Exception):
    pass


class ConditionProvider:
    name = "base"
    model = ""

    def available(self) -> bool:
        return True

    def assess(self, jpeg: bytes, side: str, extras: list[tuple[str, bytes]] | None = None) -> dict:
        """extras: (caption, jpeg) images sent after the main one, e.g. a close-up of the
        collector number or a tilted shot for surface defects."""
        h = hashlib.sha256(f"{self.name}|{self.model}|{PROMPT_VERSION}|{side}|".encode() + jpeg)
        for caption, data in extras or []:
            h.update(b"|" + caption.encode() + b"|" + data)
        key = h.hexdigest()
        path = CACHE_DIR / f"{key}.json"
        if path.exists():
            out = json.loads(path.read_text())
            out["cached"] = True
            return out
        t0 = time.time()
        result, usage = self._call(jpeg, side, extras or [])
        result = enforce_locations(result)
        out = {
            "result": result, "provider": self.name, "model": self.model, "usage": usage,
            "cost_usd": estimate_cost(self.model, usage), "seconds": round(time.time() - t0, 2),
            "cached": False,
        }
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out))
        return out

    def _call(self, jpeg: bytes, side: str, extras: list[tuple[str, bytes]]) -> tuple[dict, dict]:
        raise NotImplementedError


def enforce_locations(result: dict) -> dict:
    """Normalise model output: one entry per area (worst wins), and a defect the
    model can't place is treated as unproven.

    Unlocated "minor" calls are dropped to "none"; unlocated moderate/major calls are
    kept (a crease can span the card) but their confidence is capped at 0.5.
    """
    rank = {"none": 0, "minor": 1, "moderate": 2, "major": 3}
    best: dict[str, dict] = {}
    for a in result.get("areas", []):
        if a.get("area") not in best or rank.get(a.get("severity"), 0) > rank.get(best[a["area"]].get("severity"), 0):
            best[a.get("area")] = a
    result["areas"] = list(best.values())
    for a in result["areas"]:
        if a.get("severity") != "none" and not a.get("locations"):
            if a["severity"] == "minor":
                a["notes"] = f"(dropped: no location given) {a.get('notes', '')}".strip()
                a["severity"] = "none"
            else:
                a["confidence"] = min(float(a.get("confidence", 0)), 0.5)
    return result


def estimate_cost(model: str, usage: dict) -> float | None:
    if model not in PRICES or not usage:
        return None
    pin, pout = PRICES[model]
    return round(usage.get("input_tokens", 0) * pin / 1e6 + usage.get("output_tokens", 0) * pout / 1e6, 5)


class AnthropicProvider(ConditionProvider):
    name = "anthropic"

    def __init__(self):
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

    def available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _call(self, jpeg, side, extras):
        import anthropic
        client = anthropic.Anthropic(**_client_options())
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(jpeg).decode()}},
                    *[part for caption, data in extras for part in (
                        {"type": "text", "text": caption + ":"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.standard_b64encode(data).decode()}})],
                    {"type": "text", "text": USER.format(side=side)},
                ]}],
            )
        except anthropic.APIStatusError as e:
            raise ConditionError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ConditionError("Could not reach the Anthropic API") from e
        if resp.stop_reason == "refusal":
            raise ConditionError("The model declined to assess this image")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConditionError(f"Unparseable model output (stop_reason={resp.stop_reason})") from e
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return result, usage


class OpenAIProvider(ConditionProvider):
    name = "openai"

    def __init__(self):
        self.model = os.environ.get("OPENAI_MODEL", "gpt-5.5")

    def available(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def _call(self, jpeg, side, extras):
        import openai
        client = openai.OpenAI(**_client_options())
        url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        extra = {}
        if self.model.startswith(("gpt-5", "o3", "o4")):
            extra["reasoning_effort"] = os.environ.get("OPENAI_REASONING_EFFORT", "low")
        try:
            resp = client.chat.completions.create(
                model=self.model,
                **extra,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                        *[part for caption, data in extras for part in (
                            {"type": "text", "text": caption + ":"},
                            {"type": "image_url", "image_url": {
                                "url": "data:image/jpeg;base64," + base64.b64encode(data).decode(), "detail": "high"}})],
                        {"type": "text", "text": USER.format(side=side)},
                    ]},
                ],
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "card_condition", "strict": True, "schema": SCHEMA}},
            )
        except openai.APIError as e:
            raise ConditionError(f"OpenAI API error: {e}") from e
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise ConditionError("The model declined to assess this image")
        try:
            result = json.loads(msg.content or "")
        except json.JSONDecodeError as e:
            raise ConditionError("Unparseable model output") from e
        usage = {"input_tokens": resp.usage.prompt_tokens, "output_tokens": resp.usage.completion_tokens}
        return result, usage


class MockProvider(ConditionProvider):
    """Canned output for tests and UI demos. Never use for a real assessment."""
    name = "mock"
    model = "mock"

    def assess(self, jpeg, side, extras=None):
        return {
            "result": {
                "is_trading_card": True, "in_holder": False, "image_usable": True, "image_issues": ["DEMO DATA: mock provider"],
                "areas": [
                    {"area": "corners", "severity": "minor", "confidence": 0.6,
                     "notes": "DEMO: light whitening, top-left corner",
                     "locations": [{"label": "whitening", "x": 0.0, "y": 0.0, "w": 0.08, "h": 0.06}]},
                    {"area": "edges", "severity": "none", "confidence": 0.7, "notes": "DEMO", "locations": []},
                    {"area": "surface", "severity": "none", "confidence": 0.5, "notes": "DEMO", "locations": []},
                ],
                "summary": "DEMO output from the mock provider; not a real assessment.",
                "card": {"name_printed": "", "name_en": "", "number": "", "set_name": "", "language": "", "first_edition": False, "stamp": "", "confidence": 0.0},
            },
            "provider": "mock", "model": "mock", "usage": {}, "cost_usd": 0.0, "seconds": 0.0, "cached": False,
        }


def _client_options() -> dict:
    """Optional limits on the vision call: GRADEORSKIP_LLM_TIMEOUT (seconds) and GRADEORSKIP_LLM_RETRIES.
    Unset keeps the SDK defaults."""
    options = {}
    if os.environ.get("GRADEORSKIP_LLM_TIMEOUT"):
        options["timeout"] = float(os.environ["GRADEORSKIP_LLM_TIMEOUT"])
    if os.environ.get("GRADEORSKIP_LLM_RETRIES"):
        options["max_retries"] = int(os.environ["GRADEORSKIP_LLM_RETRIES"])
    return options


def get_provider() -> ConditionProvider | None:
    """GRADEORSKIP_LLM_PROVIDER = auto (default) | anthropic | openai | mock | none."""
    choice = os.environ.get("GRADEORSKIP_LLM_PROVIDER", "auto").lower()
    providers = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "mock": MockProvider}
    if choice == "none":
        return None
    if choice in providers:
        p = providers[choice]()
        return p if p.available() else None
    for cls in (AnthropicProvider, OpenAIProvider):
        p = cls()
        if p.available():
            return p
    return None
