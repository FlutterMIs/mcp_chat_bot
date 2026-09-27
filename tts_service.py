"""Text-to-Speech layer, decoupled from the LLM and MCP tools.

Pipeline:  final answer -> prepare_text_for_speech() -> TTSProvider.speak() -> browser audio

Providers:
- BrowserTTSProvider: Web Speech API in the user's browser. Default. Nothing leaves the machine.
- LocalTTSProvider:   OS speech engine on the server (macOS `say`, `espeak-ng`/`espeak`). Offline.
- CloudTTSProvider:   base class for hosted APIs (OpenAI, Google, Azure, ElevenLabs...).
                      OpenAITTSProvider is included as an example; it is only used when the
                      user explicitly selects it, because it sends the answer text to a third party.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import requests

UNAVAILABLE_MESSAGE = "Text-to-Speech is currently unavailable. You can continue using the text response."

# language code -> BCP-47 tags tried in order when picking a voice
LANG_TAGS = {"en": ["en-IN", "en-GB", "en-US", "en"], "hi": ["hi-IN", "hi"], "hinglish": ["en-IN", "hi-IN", "en"]}

SPEAK_LIST_ITEMS = 3      # list items spoken before "and N more"
SPEAK_TABLE_ROWS = 10     # max rows spoken when the user asks to read the table
SPEAK_CHART_POINTS = 6    # chart points spoken individually; beyond this only a summary

WORDS = {
    "en": {"crore": "crore", "lakh": "lakh", "thousand": "thousand", "rupees": "rupees", "rupee": "rupee",
           "paise": "paise", "and": "and", "minus": "minus", "percent": "percent", "million": "million",
           "billion": "billion", "more": "And {n} more", "records": "I found {n} records.",
           "highest": "The highest {col} is {val}.", "row": "Row {i}", "higher": "{a} was higher than {b}.",
           "lower": "{a} was lower than {b}.", "peak": "The highest was {p} at {v}, and the lowest was {q} at {w}.",
           "table_intro": "Here are the first {n} rows.", "table_more": "There are {n} more rows in the table."},
    "hi": {"crore": "करोड़", "lakh": "लाख", "thousand": "हज़ार", "rupees": "रुपये", "rupee": "रुपया",
           "paise": "पैसे", "and": "और", "minus": "माइनस", "percent": "प्रतिशत", "million": "मिलियन",
           "billion": "बिलियन", "more": "और {n} अन्य भी हैं", "records": "कुल {n} रिकॉर्ड मिले।",
           "highest": "सबसे ज़्यादा {col} {val} है।", "row": "पंक्ति {i}", "higher": "{a}, {b} से ज़्यादा था।",
           "lower": "{a}, {b} से कम था।", "peak": "सबसे ज़्यादा {p} में {v} था, और सबसे कम {q} में {w} था।",
           "table_intro": "पहली {n} पंक्तियाँ सुनिए।", "table_more": "टेबल में {n} और पंक्तियाँ हैं।"},
    "hinglish": {"crore": "crore", "lakh": "lakh", "thousand": "thousand", "rupees": "rupees", "rupee": "rupee",
                 "paise": "paise", "and": "aur", "minus": "minus", "percent": "percent", "million": "million",
                 "billion": "billion", "more": "aur {n} items bhi hain", "records": "Total {n} records mile.",
                 "highest": "Sabse zyada {col} {val} hai.", "row": "Row {i}", "higher": "{a}, {b} se zyada tha.",
                 "lower": "{a}, {b} se kam tha.", "peak": "Sabse zyada {p} mein {v} tha, aur sabse kam {q} mein {w} tha.",
                 "table_intro": "Pehli {n} rows sun lijiye.", "table_more": "Table mein {n} aur rows hain."},
}

HINGLISH_MARKERS = {
    "hai", "hain", "mein", "ka", "ki", "ke", "ko", "se", "kya", "kitna", "kitne", "batao", "bataiye", "sabse",
    "aur", "nahi", "nahin", "tha", "thi", "wala", "wali", "yeh", "ye", "isme", "iska", "iski", "kaun", "kis",
    "kiska", "bhai", "karo", "diya", "gaya", "raha", "rahi", "hoga", "zyada", "jyada", "kam", "mila", "mile",
}


# ---------------------------------------------------------------- language

def detect_language(text: str) -> str:
    """Return 'hi' (Devanagari), 'hinglish' (romanized Hindi) or 'en'."""
    letters = re.findall(r"[^\W\d_]", text or "")
    if not letters:
        return "en"
    devanagari = sum(1 for ch in letters if "ऀ" <= ch <= "ॿ")
    if devanagari / len(letters) > 0.3:
        return "hi"
    words = re.findall(r"[a-z]+", (text or "").lower())
    hits = sum(1 for w in words if w in HINGLISH_MARKERS)
    if words and (hits >= 2 and hits / len(words) >= 0.08):
        return "hinglish"
    return "en"


# ---------------------------------------------------------------- numbers

def _fmt_decimal(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def number_to_speech(value: float, lang: str = "en", style: str = "indian") -> str:
    """96273311.63 -> '9 crore 62 lakh 73 thousand 311 point 63' style fragments (no currency)."""
    w = WORDS.get(lang, WORDS["en"])
    neg = value < 0
    value = abs(value)
    whole = int(value)
    frac = round(value - whole, 2)
    if frac >= 1:  # rounding carried over
        whole, frac = whole + 1, 0.0
    parts = []
    if style == "indian":
        units = [(10_000_000, w["crore"]), (100_000, w["lakh"]), (1_000, w["thousand"])]
    else:
        units = [(1_000_000_000, w["billion"]), (1_000_000, w["million"]), (1_000, w["thousand"])]
    rest = whole
    for size, name in units:
        if rest >= size:
            q, rest = divmod(rest, size)
            # crore can exceed 99 (e.g. 1200 crore) — keep it as digits, TTS reads that fine
            parts.append(f"{q} {name}")
    if rest or not parts:
        parts.append(str(rest))
    out = " ".join(parts)
    if frac:
        out += (" दशमलव " if lang == "hi" else " point ") + f"{frac:.2f}"[2:].rstrip("0")
    return (w["minus"] + " " if neg else "") + out


def currency_to_speech(value: float, lang: str = "en", style: str = "indian", with_paise: bool = True) -> str:
    """96273311.63 -> '9 crore 62 lakh 73 thousand 311 rupees and 63 paise'."""
    w = WORDS.get(lang, WORDS["en"])
    neg = value < 0
    value = abs(value)
    value = round(value, 2) if with_paise else round(value)
    rupees = int(value)
    paise = int(round((value - rupees) * 100))
    if paise == 100:
        rupees, paise = rupees + 1, 0
    text = number_to_speech(rupees, lang, style) + " " + (w["rupee"] if rupees == 1 else w["rupees"])
    if paise:
        text += f" {w['and']} {paise} {w['paise']}"
    return (w["minus"] + " " if neg else "") + text


_UNIT_MULT = {"crore": 1e7, "crores": 1e7, "cr": 1e7, "करोड़": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
              "lacs": 1e5, "l": 1e5, "लाख": 1e5, "k": 1e3, "thousand": 1e3, "हज़ार": 1e3, "हजार": 1e3,
              "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}
_UNIT_RE = r"(?:crores?|cr|करोड़|lakhs?|lacs?|लाख|thousand|हज़ार|हजार|million|mn|billion|bn|k|l|m|b)(?![\w\u0900-\u097F])"
_NUM_RE = r"-?\d[\d,]*(?:\.\d+)?"
_CURRENCY_RE = re.compile(
    rf"(?:₹|\bRs\.?|\bINR)\s*(?P<num>{_NUM_RE})(?:\s*(?P<unit>{_UNIT_RE}))?(?:\s*(?:rupees|rupaye|रुपये))?",
    re.IGNORECASE,
)
_UNIT_NUMBER_RE = re.compile(rf"(?<![\w.])(?P<num>{_NUM_RE})\s*(?P<unit>crores?|cr|करोड़|lakhs?|lacs?|लाख)(?![\w\u0900-\u097F])", re.IGNORECASE)
_PERCENT_RE = re.compile(rf"(?P<num>{_NUM_RE})\s*%")
_PLAIN_NUMBER_RE = re.compile(rf"(?<![\w.\-/:])(?P<num>{_NUM_RE})(?![\w/:])")


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def speak_numbers(text: str, lang: str = "en", style: str = "indian") -> str:
    """Rewrite currency, percentages and large numbers into natural spoken form."""
    w = WORDS.get(lang, WORDS["en"])

    def cur(m):
        value = _to_float(m.group("num"))
        unit = (m.group("unit") or "").lower()
        if unit:
            # "₹9.62 crore" is already rounded; don't invent paise
            return currency_to_speech(value * _UNIT_MULT.get(unit, 1), lang, style, with_paise=False)
        return currency_to_speech(value, lang, style)

    def unit_number(m):
        unit = m.group("unit").lower()
        return number_to_speech(round(_to_float(m.group("num")) * _UNIT_MULT.get(unit, 1), 2), lang, style)

    def pct(m):
        return f"{_fmt_decimal(_to_float(m.group('num')))} {w['percent']}"

    def plain(m):
        raw = m.group("num")
        value = _to_float(raw)
        # leave years, IDs and small counts alone ("2026", "333 records"); only rewrite big amounts
        if abs(value) >= 100_000:
            return number_to_speech(value, lang, style)
        return raw.replace(",", "")

    text = _CURRENCY_RE.sub(cur, text)
    text = _UNIT_NUMBER_RE.sub(unit_number, text)
    text = _PERCENT_RE.sub(pct, text)
    return _PLAIN_NUMBER_RE.sub(plain, text)


# ---------------------------------------------------------------- cleanup

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U000020E3\U00002190-\U000021FF\U00002300-\U000023FF]+"
)
_SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|WITH)\b[\s\S]*?\b(FROM|INTO|SET|AS)\b[\s\S]*?(;|$)", re.IGNORECASE | re.MULTILINE)
_INTERNAL_LINE_RE = re.compile(
    r"^\s*(?:plan|tool|mcp|debug|sql|query|json|trace|source_id|sheet_name|metric|aggregation|group_by|filters)\s*[:=]",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _strip_json(text: str) -> str:
    """Drop balanced {...} / [...] blocks that look like JSON."""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch in "{[":
            close = "}" if ch == "{" else "]"
            depth, j = 0, i
            while j < len(text):
                if text[j] == ch:
                    depth += 1
                elif text[j] == close:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            block = text[i:j + 1]
            if depth == 0 and ('":' in block or "':" in block or block.count(",") >= 2 and '"' in block):
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def prepare_text_for_speech(response: str, language: str | None = None, number_style: str = "indian",
                            summarize_lists: bool = True) -> str:
    """Turn a displayed answer into text that sounds natural when spoken.

    Removes markdown, emojis, code, JSON, SQL, tool/debug lines and markdown tables, shortens long
    lists and rewrites currency/numbers (₹96,273,311.63 -> 9 crore 62 lakh 73 thousand 311 rupees and 63 paise).
    """
    if not response:
        return ""
    lang = language or detect_language(response)
    w = WORDS.get(lang, WORDS["en"])
    text = str(response)
    text = re.sub(r"```[\s\S]*?```", " ", text)            # fenced code
    text = re.sub(r"`[^`\n]*`", " ", text)                 # inline code
    text = re.sub(r"<[^>\n]+>", " ", text)                 # html tags
    text = _strip_json(text)
    text = _SQL_RE.sub(" ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)      # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)   # links -> label
    text = re.sub(r"https?://\S+", " ", text)
    text = _EMOJI_RE.sub(" ", text)

    lines, items = [], []

    def flush_items():
        if not items:
            return
        keep = items if not summarize_lists or len(items) <= SPEAK_LIST_ITEMS + 1 else items[:SPEAK_LIST_ITEMS]
        lines.extend(keep)
        if len(keep) < len(items):
            lines.append(w["more"].format(n=len(items) - len(keep)) + ".")
        items.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or _INTERNAL_LINE_RE.match(line):
            flush_items()
            continue
        if line.startswith("|") or re.fullmatch(r"[\s|:+\-=_*]{3,}", line):   # markdown tables / rules
            flush_items()
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^>\s*", "", line)
        is_item = bool(_LIST_ITEM_RE.match(line))
        line = _LIST_ITEM_RE.sub("", line)
        line = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", line)
        line = re.sub(r"(?<![\w*])[*_](.+?)[*_](?![\w*])", r"\1", line)
        line = line.replace("*", "").replace("#", " ")
        if not re.search(r"[.!?।:;,]$", line):
            line += "."
        if is_item:
            items.append(line)
        else:
            flush_items()
            lines.append(line)
    flush_items()

    text = " ".join(lines)
    text = speak_numbers(text, lang, number_style)
    text = re.sub(r"\s*:\s+", ": ", text)
    text = re.sub(r"\s+([.,!?;:।])", r"\1", text)
    text = re.sub(r"([.!?।])(?:\s*[.!?।])+", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# ---------------------------------------------------------------- tables & charts

_READ_TABLE_RE = re.compile(
    r"(table|rows?|records?|list|टेबल|तालिका).{0,25}(read|padh|parh|suna|bol|पढ़|सुना|बोल)"
    r"|(read|padh|parh|suna|bol|पढ़|सुना)\w*.{0,25}(table|rows?|records?|टेबल)",
    re.IGNORECASE,
)


def wants_table_read(question: str) -> bool:
    """'table read karke sunao', 'read the table', 'rows padh ke sunao' ..."""
    return bool(_READ_TABLE_RE.search(question or ""))


def _speak_value(v, lang, style, currency=False):
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if v != v:  # NaN
            return ""
        return currency_to_speech(float(v), lang, style) if currency else number_to_speech(float(v), lang, style)
    return str(v)


def _speak_period(p) -> str:
    s = str(p)
    for fmt, out in (("%Y-%m", "%B %Y"), ("%Y-%m-%d", "%d %B %Y")):
        try:
            return datetime.strptime(s, fmt).strftime(out).lstrip("0")
        except ValueError:
            pass
    return s


def _is_money(col: str | None) -> bool:
    return bool(col) and bool(re.search(r"amount|sales|revenue|price|value|rate|total|cost|turnover", str(col), re.I))


def table_to_speech(rows: list[dict], lang: str = "en", style: str = "indian", money_metric: bool = False,
                    max_rows: int = SPEAK_TABLE_ROWS) -> str:
    """Read the first rows as short sentences (used only when the user explicitly asks)."""
    if not rows:
        return ""
    w = WORDS.get(lang, WORDS["en"])
    shown = rows[:max_rows]
    parts = [w["table_intro"].format(n=len(shown))]
    for i, row in enumerate(shown, 1):
        cells = []
        for col, val in row.items():
            is_money = (col == "value" and money_metric) or (col != "value" and _is_money(col))
            spoken = _speak_value(val, lang, style, currency=is_money)
            if spoken:
                label = "" if col in {"value", "period"} else f"{col} "
                cells.append(f"{label}{_speak_period(spoken) if col == 'period' else spoken}")
        parts.append(f"{w['row'].format(i=i)}: " + ", ".join(cells) + ".")
    if len(rows) > len(shown):
        parts.append(w["table_more"].format(n=len(rows) - len(shown)))
    return " ".join(parts)


def chart_to_speech(rows: list[dict], x: str, y: str, lang: str = "en", style: str = "indian",
                    money: bool = True) -> str:
    """Describe a chart in words instead of reading coordinates."""
    w = WORDS.get(lang, WORDS["en"])
    pts = [(r.get(x), r.get(y)) for r in rows if isinstance(r.get(y), (int, float)) and r.get(y) == r.get(y)]
    if len(pts) < 2:
        return ""
    label = (lambda p: _speak_period(p)) if x == "period" else str
    say = lambda v: _speak_value(v, lang, style, currency=money)
    if len(pts) <= SPEAK_CHART_POINTS:
        body = ", ".join(f"{label(p)}: {say(v)}" for p, v in pts) + "."
    else:
        hi = max(pts, key=lambda t: t[1])
        lo = min(pts, key=lambda t: t[1])
        body = w["peak"].format(p=label(hi[0]), v=say(hi[1]), q=label(lo[0]), w=say(lo[1]))
    if x == "period":  # compare the last two periods, which is what people usually care about
        (pa, va), (pb, vb) = pts[-2], pts[-1]
        if vb != va:
            body += " " + w["higher" if vb > va else "lower"].format(a=label(pb), b=label(pa))
    return body


def build_speech_text(answer: str, *, question: str = "", rows: list[dict] | None = None, chart: dict | None = None,
                      metric: str | None = None, language: str | None = None, number_style: str = "indian") -> tuple[str, str]:
    """Compose what gets spoken for one assistant message. Returns (speech_text, language)."""
    lang = language or detect_language(answer)
    rows = rows or []
    read_table = wants_table_read(question)
    speech = prepare_text_for_speech(answer, lang, number_style, summarize_lists=not read_table)
    money = _is_money(metric)
    extra = ""
    if read_table and rows:
        extra = table_to_speech(rows, lang, number_style, money_metric=money)
    elif chart and rows:
        extra = chart_to_speech(rows, chart.get("x"), chart.get("y"), lang, number_style, money)
    elif len(rows) > SPEAK_TABLE_ROWS:
        w = WORDS.get(lang, WORDS["en"])
        count_said = re.search(rf"\b{len(rows)}\b", speech)
        extra = "" if count_said else w["records"].format(n=len(rows))
        col = metric if metric in rows[0] else ("value" if "value" in rows[0] else None)
        if col:
            vals = [r[col] for r in rows if isinstance(r.get(col), (int, float)) and r.get(col) == r.get(col)]
            if vals:
                extra += " " + w["highest"].format(col=metric or col, val=_speak_value(max(vals), lang, number_style, money))
    if extra:
        speech = f"{speech} {extra}".strip()
    return speech, lang


# ---------------------------------------------------------------- providers

class TTSUnavailable(RuntimeError):
    pass


@dataclass
class SpeechResult:
    provider: str
    text: str
    language: str
    lang_tags: list[str]
    voice: str | None = None
    speed: float = 1.0
    volume: float = 1.0
    audio: bytes | None = None          # None -> the browser synthesizes `text` itself
    mime: str = "audio/wav"
    meta: dict = field(default_factory=dict)

    @property
    def client_side(self) -> bool:
        return self.audio is None


class TTSProvider(ABC):
    name: str = "base"
    label: str = "Base"
    sends_data_externally: bool = False

    def is_available(self) -> bool:
        return True

    def list_voices(self) -> list[str]:
        return []

    @abstractmethod
    def speak(self, text: str, language: str = "en", voice: str | None = None, speed: float = 1.0,
              volume: float = 1.0) -> SpeechResult: ...


class BrowserTTSProvider(TTSProvider):
    """Web Speech API. Returns no audio: the page speaks `text` with the browser's own voices."""
    name, label = "browser", "Browser (private, on-device)"

    def speak(self, text, language="en", voice=None, speed=1.0, volume=1.0):
        return SpeechResult(self.name, text, language, LANG_TAGS.get(language, LANG_TAGS["en"]), voice, speed, volume)


class LocalTTSProvider(TTSProvider):
    """Offline OS speech engine on the machine running Streamlit (macOS `say`, or espeak-ng/espeak)."""
    name, label = "local", "Local system voice (offline)"

    def _engine(self):
        for exe in ("say", "espeak-ng", "espeak"):
            if shutil.which(exe):
                return exe
        return None

    def is_available(self):
        return self._engine() is not None

    def list_voices(self):
        exe = self._engine()
        try:
            if exe == "say":
                out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
                return [re.split(r"\s{2,}", l.strip())[0] for l in out.splitlines() if l.strip()]
            if exe:
                out = subprocess.run([exe, "--voices"], capture_output=True, text=True, timeout=10).stdout
                return [l.split()[3] for l in out.splitlines()[1:] if len(l.split()) > 3]
        except Exception:
            pass
        return []

    def speak(self, text, language="en", voice=None, speed=1.0, volume=1.0):
        exe = self._engine()
        if not exe:
            raise TTSUnavailable("No local speech engine found (install espeak-ng, or use macOS).")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "speech.wav")
            if exe == "say":
                cmd = ["say", "-r", str(int(185 * speed)), "--data-format=LEI16@22050", "-o", out]
                if voice:
                    cmd += ["-v", voice]
            else:
                cmd = [exe, "-s", str(int(170 * speed)), "-a", str(int(100 * volume)), "-w", out,
                       "-v", voice or ("hi" if language == "hi" else "en")]
            try:
                subprocess.run(cmd + [text], check=True, capture_output=True, timeout=120)
                with open(out, "rb") as f:
                    audio = f.read()
            except Exception as e:
                raise TTSUnavailable(f"Local speech engine failed: {e}") from e
        return SpeechResult(self.name, text, language, LANG_TAGS.get(language, LANG_TAGS["en"]), voice, speed,
                            volume, audio=audio, mime="audio/wav")


class CloudTTSProvider(TTSProvider):
    """Base class for hosted TTS APIs. Subclasses implement _synthesize(); everything else is shared.
    Add Google Cloud / Azure / ElevenLabs by subclassing this and registering in PROVIDERS."""
    sends_data_externally = True
    api_key_env: str = ""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv(self.api_key_env, "")

    def is_available(self):
        return bool(self.api_key)

    @abstractmethod
    def _synthesize(self, text: str, language: str, voice: str | None, speed: float) -> tuple[bytes, str]: ...

    def speak(self, text, language="en", voice=None, speed=1.0, volume=1.0):
        if not self.is_available():
            raise TTSUnavailable(f"{self.label}: API key not configured ({self.api_key_env}).")
        try:
            audio, mime = self._synthesize(text, language, voice, speed)
        except Exception as e:
            raise TTSUnavailable(f"{self.label} failed: {e}") from e
        return SpeechResult(self.name, text, language, LANG_TAGS.get(language, LANG_TAGS["en"]), voice, speed,
                            volume, audio=audio, mime=mime)


class OpenAITTSProvider(CloudTTSProvider):
    name, label, api_key_env = "openai", "OpenAI TTS (cloud)", "OPENAI_API_KEY"
    voices = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]

    def list_voices(self):
        return self.voices

    def _synthesize(self, text, language, voice, speed):
        r = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"), "input": text[:4000],
                  "voice": voice if voice in self.voices else "coral", "speed": max(0.25, min(4.0, speed)),
                  "response_format": "mp3"},
            timeout=60,
        )
        r.raise_for_status()
        return r.content, "audio/mpeg"


PROVIDERS: dict[str, type[TTSProvider]] = {
    BrowserTTSProvider.name: BrowserTTSProvider,
    LocalTTSProvider.name: LocalTTSProvider,
    OpenAITTSProvider.name: OpenAITTSProvider,
}


def get_provider(name: str = "browser") -> TTSProvider:
    return PROVIDERS.get(name, BrowserTTSProvider)()


def available_providers() -> dict[str, str]:
    out = {}
    for name, cls in PROVIDERS.items():
        try:
            if cls().is_available():
                out[name] = cls.label
        except Exception:
            pass
    return out


def speak(text: str, language: str | None = None, voice: str | None = None, speed: float = 1.0,
          volume: float = 1.0, provider: str = "browser") -> SpeechResult:
    """Provider-agnostic entry point. Raises TTSUnavailable; callers should degrade to text-only."""
    if not text or not text.strip():
        raise TTSUnavailable("Nothing to speak.")
    lang = language or detect_language(text)
    return get_provider(provider).speak(text, lang, voice, speed, volume)
