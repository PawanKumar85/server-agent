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
    r"\bm3u8\b": "M-3-U-8 playlist",
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
    r"\b404\b": "four zero four not found",
    r"\b500\b": "five hundred internal server error",
    r"\b502\b": "five zero two bad gateway",
    r"\b503\b": "five zero three service unavailable",
    r"\b504\b": "five zero four gateway timeout",
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
    """Expands domain dots to phonetic [dot] with clean letter spacing."""
    if not text:
        return ""

    def _replace_url(match):
        raw = match.group(0)
        # Avoid double replacing
        if "[dot]" in raw:
            return raw
        # Replace dot with phonetic pause
        return raw.replace(".", " [dot] ")

    # Match domain-like patterns: e.g. cdn.ottlive.co.in or 192.168.1.1
    domain_re = r'([a-zA-Z0-9_\-]+(?:\.[a-zA-Z0-9_\-]+)+(?::\d+)?)'
    return re.sub(domain_re, _replace_url, text)


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
