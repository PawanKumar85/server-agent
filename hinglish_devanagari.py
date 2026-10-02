"""Roman-script Hinglish → what a Hindi voice should read.

The neural Hindi voices (Piper, voice_neural.py) pronounce Devanagari correctly but read Roman script with English
rules ("hai" becomes "high", "dhakkan" loses its aspiration). So every Hindi word the alerts use is written in
Devanagari here; English words (encoder, stream, server, channel, ...) and names (Rang Manch, tnpnews, xcode4) stay
in Latin script, which the voice reads the English way, as people in a control room actually say them.
Unknown words are left as they are.
"""

import re

# Phrases first (they read differently as a whole), then single words.
PHRASES = {
    "kya hai bhai": "क्या है भाई",
    "kuch nhi aata tere ko": "कुछ नहीं आता तेरे को",
    "ullu ke patthe": "उल्लू के पट्ठे",
    "bhaad mein gaya": "भाड़ में गया",
    "heads up": "heads up",
    "good news": "good news",
    "what the hell": "what the hell",
    "bloody hell": "bloody hell",
    "damn it": "damn it",
}

WORDS = {
    "aa": "आ", "aage": "आगे", "aane": "आने", "aap": "आप", "aata": "आता", "ab": "अब", "abe": "अबे", "abhi": "अभी",
    "agli": "अगली", "aksar": "अक्सर", "apne": "अपने", "arre": "अरे", "asar": "असर", "asli": "असली", "atak": "अटक",
    "aur": "और", "baaki": "बाकी", "baar": "बार", "baat": "बात", "badh": "बढ़", "bahut": "बहुत", "bakchodi": "बकचोदी",
    "bakwaas": "बकवास", "band": "बंद", "bas": "बस", "bataunga": "बताऊँगा", "bewakoof": "बेवकूफ़", "bhaad": "भाड़",
    "bhai": "भाई", "bhejo": "भेजो", "bhi": "भी", "bilkul": "बिल्कुल", "bina": "बिना", "bolna": "बोलना",
    "chal": "चल", "chalao": "चलाओ", "chale": "चले", "chalega": "चलेगा", "chalo": "चलो", "chhod": "छोड़",
    "chhoti": "छोटी", "chhota": "छोटा", "chutiyapa": "चूतियापा", "daal": "डाल", "de": "दे", "dekh": "देख", "dekho": "देखो",
    "dena": "देना", "der": "देर", "dhakkan": "ढक्कन", "dhyan": "ध्यान", "dijiye": "दीजिए", "dikh": "दिख",
    "dikhai": "दिखाई", "dikkat": "दिक्कत", "dimaag": "दिमाग", "ek": "एक", "gadbad": "गड़बड़", "gaya": "गया",
    "gaye": "गए", "gayi": "गई", "ghanta": "घंटा", "ghante": "घंटे", "gir": "गिर", "hai": "है", "hain": "हैं",
    "halki": "हल्की", "hi": "ही", "ho": "हो", "hoon": "हूँ", "hota": "होता", "hua": "हुआ", "isko": "इसको",
    "jaldi": "जल्दी", "jayenge": "जाएँगे", "jhatke": "झटके", "ji": "जी", "jo": "जो", "ka": "का", "kaam": "काम",
    "kam": "कम", "kamchor": "कामचोर", "kar": "कर", "karo": "करो", "ke": "के", "kharab": "ख़राब", "ki": "की",
    "kitni": "कितनी", "kiya": "किया", "kiye": "किए", "ko": "को", "koi": "कोई", "kuch": "कुछ", "kya": "क्या",
    "lagao": "लगाओ", "lao": "लाओ", "lenge": "लेंगे", "lijiye": "लीजिए", "likh": "लिख", "lo": "लो", "log": "लोग",
    "main": "मैं", "mat": "मत", "mazaak": "मज़ाक", "mein": "में", "mil": "मिल", "na": "न", "nahi": "नहीं",
    "nhi": "नहीं", "nalayak": "नालायक", "nazar": "नज़र", "neeche": "नीचे", "pada": "पड़ा", "padega": "पड़ेगा",
    "pata": "पता", "patthe": "पट्ठे", "pe": "पे", "peeche": "पीछे", "pehle": "पहले", "phir": "फिर",
    "pichli": "पिछली", "raha": "रहा", "rahe": "रहे", "rahi": "रही", "ruk": "रुक", "saala": "साला", "sab": "सब",
    "sahi": "सही", "se": "से", "seedha": "सीधा", "shukr": "शुक्र", "shukriya": "शुक्रिया", "si": "सी",
    "suniye": "सुनिए", "suno": "सुनो", "tak": "तक", "tere": "तेरे", "tha": "था", "theek": "ठीक", "thoda": "थोड़ा",
    "thodi": "थोड़ी", "toh": "तो", "turant": "तुरंत", "ullu": "उल्लू", "upar": "ऊपर", "wahi": "वही",
    "wajah": "वजह", "wala": "वाला", "wale": "वाले", "wali": "वाली", "wapas": "वापस", "ya": "या", "yaar": "यार",
    "ye": "ये", "zara": "ज़रा", "zarurat": "ज़रूरत", "zyada": "ज़्यादा", "dikhta": "दिखता", "sirf": "सिर्फ़",
    "anushrav": "अनुश्रव", "jawab": "जवाब", "dheere": "धीरे", "badhi": "बढ़ी", "hui": "हुई", "doosre": "दूसरे",
    "tukde": "टुकड़े", "barabar": "बराबर", "lambai": "लंबाई", "khatam": "ख़त्म", "kab": "कब", "nahi": "नहीं", "aaj": "आज", "kal": "कल", "chalu": "चालू", "kaise": "कैसे", "kyun": "क्यों", "sun": "सुन", "baje": "बजे",
}
# "main" is also English ("main feed", "main input"): read it as Hindi "मैं" only when it isn't followed by those.
_ENGLISH_AFTER_MAIN = re.compile(r"\bmain(?=\s+(?:feed|input|server|link|stream|source)\b)", re.I)

_PHRASE_RE = re.compile("|".join(sorted((re.escape(p) for p in PHRASES), key=len, reverse=True)), re.I)
_WORD_RE = re.compile(r"[A-Za-z]+")


def to_speakable(text: str) -> str:
    """The alert line as a Hindi voice should read it: Hindi words in Devanagari, the rest unchanged."""
    if not text:
        return ""
    text = _ENGLISH_AFTER_MAIN.sub("Qmainenglishq", text)
    text = _PHRASE_RE.sub(lambda m: PHRASES[m.group(0).lower()], text)

    def word(m):
        w = m.group(0)
        if w == "Qmainenglishq":
            return "main"
        return WORDS.get(w.lower(), w)
    text = _WORD_RE.sub(word, text)
    text = text.replace("...", ", ").replace("..", ", ")  # a pause, not "dot dot dot"
    return re.sub(r"\s+", " ", text).strip()
