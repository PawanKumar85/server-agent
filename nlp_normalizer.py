"""NLP Hinglish Text Normalizer & Phonetic Expansion Engine for TTS Speech Alerts.

Converts technical telecom strings, acronyms, network metrics, and domain URLs into
acoustically clear, natural Hinglish broadcast text for browser and neural TTS synthesizers.
"""

import re
from typing import Dict, Optional

# Acronyms and technical terms phonetic expansion
PHONETIC_ACRONYMS: Dict[str, str] = {
    r"\bcdn\b": "C-D-N",
    r"\bott\b": "O-T-T",
    r"\bottlive\b": "O-T-T Live",
    r"\bhls\b": "H-L-S",
    r"\brtmp\b": "R-T-M-P",
    r"\bm3u8\b(?!\s+playlist)": "M-3-U-8 playlist",
    r"\bm3u8\b": "M-3-U-8",
    r"\bicmp\b": "I-C-M-P ping",
    r"\brtt\b": "R-T-T",
    r"\bscte-?35\b": "S-C-T-E thirty five ad cue",
    r"\bfps\b": "frames per second",
    r"\bkbps\b": "K-B-P-S",
    r"\bmbps\b": "M-B-P-S",
    r"\brca\b": "root cause analysis",
    r"\bmtbf\b": "M-T-B-F",
    r"\bmttr\b": "M-T-T-R",
}

# HTTP status codes phonetic mappings in Hindi/Hinglish
HTTP_STATUS_MAP: Dict[str, str] = {
    r"\b404\b": "four zero four",  # the sentence already says what it means ("404 error", "404 aa raha hai")
    r"\b500\b": "five hundred",
    r"\b502\b": "five zero two",
    r"\b503\b": "five zero three",
    r"\b504\b": "five zero four",
}

# Number conversions in common telemetry units
UNIT_PATTERNS = [
    (r"(\d+)\s*ms\b", r"\1 millisecond"),
    (r"(\d+)\s*s\b", r"\1 second"),
    (r"(\d+)\s*min\b", r"\1 minute"),
    (r"(\d+)\s*hr\b", r"\1 ghante"),
    (r"100%", "hundred percent"),
]


def normalize_domain_url(text: str) -> str:
    """Says servers the way people do, by their short name: "cdn.ottlive.co.in" and "cdn [dot] ottlive [dot] co
    [dot] in" both become "cdn"; "cdn.ottlive.co.in/Rang Manch" becomes the channel, "Rang Manch". Numbers are
    numbers: 98.5 -> "98 point 5". An IP address is read digit group by digit group."""
    if not text:
        return ""
    text = re.sub(r"\b([A-Za-z0-9_-]+)(?:\s*\[\s*dot\s*\]\s*[A-Za-z0-9_-]+)+", r"\1", text)  # already phonetic
    text = re.sub(r"\b[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+/(?=[A-Z])", "", text)  # host/Channel -> Channel

    def _replace(match):
        raw = match.group(0)
        num = re.fullmatch(r"(\d+)\.(\d+)([a-zA-Z%]*)", raw)
        if num:  # a decimal, not a domain (units are expanded later)
            return f"{num.group(1)} point {num.group(2)}{num.group(3)}"
        if re.fullmatch(r"\d+(?:\.\d+){3}(?::\d+)?", raw):  # an IP address
            return raw.replace(".", " ")
        return raw.split(".")[0]  # a host: its short name

    return re.sub(r"[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+(?::\d+)?", _replace, text)


def normalize_hinglish_speech(text: str) -> str:
    """Transforms raw alert string into natural, phonetically optimized Hinglish broadcast speech."""
    if not text:
        return ""

    normalized = str(text)

    # 1. Normalize domain names and host URLs
    normalized = normalize_domain_url(normalized)

    # 2. Expand common OTT and telecom acronyms (case-insensitive)
    for pattern, phonetic in PHONETIC_ACRONYMS.items():
        normalized = re.sub(pattern, phonetic, normalized, flags=re.IGNORECASE)

    # 3. Expand HTTP status codes with clear pronunciation
    for pattern, phonetic in HTTP_STATUS_MAP.items():
        normalized = re.sub(pattern, phonetic, normalized)

    # 4. Expand telemetry duration and rate units
    for pattern, replacement in UNIT_PATTERNS:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    # 5. Clean up duplicate spaces and brackets
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def phonetic_channel_name(channel: str) -> str:
    """Prettifies channel name for speech (e.g., 'punjabshort' -> 'Punjab Short')."""
    if not channel:
        return "Channel"
    ch = str(channel).strip()
    # Common channel word breaks
    special_names = {
        "punjabshort": "Punjab Short",
        "tnpnews": "T-N-P News",
        "abnandhrajyothy": "A-B-N Andhra Jyothy",
        "gtcpunjabi": "G-T-C Punjabi",
        "lokmatbharat": "Lokmat Bharat",
        "nammatv": "Namma T-V",
        "nebharat24": "N-E Bharat 24",
    }
    return special_names.get(ch.lower(), ch)
