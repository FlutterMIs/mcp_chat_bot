"""Streamlit UI for Text-to-Speech: per-message player controls and the browser voice list.

Browser provider: speech runs in the page via the Web Speech API (Speak / Pause / Resume / Stop).
Server providers (local, cloud): audio bytes are generated on demand and played with st.audio.
Any failure here degrades to a short notice; it never breaks the chat.
"""
import streamlit as st

from tts_service import UNAVAILABLE_MESSAGE, TTSUnavailable, speak

_PLAYER_HTML = """
<div class="tts">
  <button type="button" data-act="speak">🔊 Speak</button>
  <button type="button" data-act="pause">⏸ Pause</button>
  <button type="button" data-act="resume">▶ Resume</button>
  <button type="button" data-act="stop">⏹ Stop</button>
  <span class="status"></span>
</div>
"""

_PLAYER_CSS = """
.tts { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; font-family: var(--st-font); }
.tts button {
  font: inherit; font-size: 0.85rem; padding: 0.2rem 0.65rem; cursor: pointer;
  color: var(--st-text-color); background: var(--st-secondary-background-color);
  border: 1px solid var(--st-border-color); border-radius: var(--st-button-radius, 0.5rem);
}
.tts button:hover:not(:disabled) { border-color: var(--st-primary-color); color: var(--st-primary-color); }
.tts button:disabled { opacity: 0.45; cursor: default; }
.tts .status { font-size: 0.8rem; color: var(--st-gray-text-color, var(--st-text-color)); opacity: 0.8; }
"""

# One speechSynthesis queue per page; players coordinate through window.__tts.
_PLAYER_JS = """
const UNAVAILABLE = "Text-to-Speech is currently unavailable. You can continue using the text response."

function chunks(text) {
  // Chrome cuts off long utterances, so speak sentence-sized pieces.
  const parts = text.match(/[^.!?।]+[.!?।]*\\s*/g) || [text]
  const out = []
  let buf = ""
  for (const p of parts) {
    if ((buf + p).length > 220 && buf) { out.push(buf); buf = "" }
    buf += p
  }
  if (buf.trim()) out.push(buf)
  return out
}

function pickVoice(voices, name, tags) {
  if (name) {
    const v = voices.find(v => v.name === name)
    if (v) return v
  }
  const lang = v => (v.lang || "").toLowerCase().replace("_", "-")
  for (const tag of tags || []) {
    const v = voices.find(v => lang(v) === tag.toLowerCase())
    if (v) return v
  }
  for (const tag of tags || []) {
    const base = tag.split("-")[0].toLowerCase()
    const v = voices.find(v => lang(v).split("-")[0] === base)
    if (v) return v
  }
  return null
}

export default function (component) {
  const { data, parentElement } = component
  const root = parentElement.querySelector(".tts")
  const status = parentElement.querySelector(".status")
  const btn = act => parentElement.querySelector(`[data-act="${act}"]`)
  const g = (window.__tts = window.__tts || { active: null, played: new Set(), listeners: new Set() })
  const synth = window.speechSynthesis

  if (!synth || typeof SpeechSynthesisUtterance === "undefined") {
    root.querySelectorAll("button").forEach(b => (b.disabled = true))
    status.textContent = UNAVAILABLE
    return
  }

  const id = data.id
  const setState = state => {
    const mine = g.active === id
    btn("pause").disabled = !(mine && state === "speaking")
    btn("resume").disabled = !(mine && state === "paused")
    btn("stop").disabled = !(mine && (state === "speaking" || state === "paused"))
    status.textContent = mine ? ({ speaking: "Speaking…", paused: "Paused", error: UNAVAILABLE }[state] || "") : ""
  }
  const listener = state => setState(state)
  g.listeners.add(listener)
  const broadcast = state => g.listeners.forEach(fn => fn(state))

  const start = () => {
    const text = (data.text || "").trim()
    if (!text) return
    synth.cancel()
    g.active = id
    const voices = synth.getVoices()
    const voice = pickVoice(voices, data.voice, data.lang_tags)
    const pieces = chunks(text)
    pieces.forEach((piece, i) => {
      const u = new SpeechSynthesisUtterance(piece)
      if (voice) { u.voice = voice; u.lang = voice.lang } else if (data.lang_tags) { u.lang = data.lang_tags[0] }
      u.rate = data.rate || 1
      u.volume = data.volume ?? 1
      if (i === 0) u.onstart = () => broadcast("speaking")
      if (i === pieces.length - 1) u.onend = () => { if (g.active === id) { g.active = null; broadcast("idle") } }
      u.onerror = e => {
        if (e.error === "interrupted" || e.error === "canceled") return
        broadcast("error")
      }
      synth.speak(u)
    })
    broadcast("speaking")
  }

  btn("speak").onclick = start
  btn("pause").onclick = () => { if (g.active === id) { synth.pause(); broadcast("paused") } }
  btn("resume").onclick = () => { if (g.active === id) { synth.resume(); broadcast("speaking") } }
  btn("stop").onclick = () => { if (g.active === id) { synth.cancel(); g.active = null; broadcast("idle") } }

  setState(g.active === id ? (synth.paused ? "paused" : "speaking") : "idle")

  // Auto Speak: play a freshly generated answer exactly once, not on every rerun.
  if (data.autoplay && !g.played.has(id)) {
    g.played.add(id)
    const go = () => start()
    if (synth.getVoices().length) go()
    else setTimeout(go, 250)
  }
  g.played.add(id)

  return () => g.listeners.delete(listener)
}
"""

_VOICES_JS = """
export default function (component) {
  const { data, setStateValue } = component
  const synth = window.speechSynthesis
  if (!synth) { setStateValue("voices", []); return }
  const report = () => {
    const voices = synth.getVoices().map(v => ({ name: v.name, lang: v.lang, local: v.localService }))
    if (!voices.length) return
    const sig = voices.map(v => v.name).join("|")
    if (sig !== (data && data.known)) setStateValue("voices", voices)
  }
  report()
  synth.addEventListener("voiceschanged", report)
  return () => synth.removeEventListener("voiceschanged", report)
}
"""

_player = st.components.v2.component("tts_player", html=_PLAYER_HTML, css=_PLAYER_CSS, js=_PLAYER_JS)
_voices = st.components.v2.component("tts_voices", html="<span></span>\n", js=_VOICES_JS)


def browser_voices(key="tts_voice_probe"):
    """Mount the invisible voice probe and return the browser's voices as [{name, lang, local}]."""
    try:
        known = "|".join(v["name"] for v in (st.session_state.get(key, {}) or {}).get("voices", []) or [])
        result = _voices(key=key, data={"known": known}, default={"voices": []}, on_voices_change=lambda: None)
        return list(result.voices or [])
    except Exception:
        return []


def speech_controls(msg_id, text, lang_tags, *, provider="browser", voice=None, speed=1.0, volume=1.0,
                    autoplay=False, language="en"):
    """Render TTS controls under one assistant message. Never raises."""
    try:
        if not text:
            return
        if provider == "browser":
            _player(key=f"tts_{msg_id}", data={
                "id": msg_id, "text": text, "lang_tags": lang_tags, "voice": voice,
                "rate": speed, "volume": volume, "autoplay": autoplay,
            })
            return
        cache = st.session_state.setdefault("tts_audio", {})
        entry = cache.get(msg_id)
        clicked = st.button("🔊 Speak", key=f"tts_btn_{msg_id}")
        if clicked or (autoplay and entry is None):
            result = speak(text, language, voice, speed, volume, provider=provider)
            entry = cache[msg_id] = {"audio": result.audio, "mime": result.mime, "fresh": True}
        if entry:
            st.audio(entry["audio"], format=entry["mime"], autoplay=entry.pop("fresh", False))
    except TTSUnavailable as e:
        st.caption(f"{UNAVAILABLE_MESSAGE} ({e})")
    except Exception:
        st.caption(UNAVAILABLE_MESSAGE)
