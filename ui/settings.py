"""Settings page: preferences that follow the user across devices, WhatsApp linking, model override, debug."""
from __future__ import annotations

import os

import streamlit as st

from backend import preferences, whatsapp_link
from tts_service import available_providers, get_provider
from tts_ui import browser_voices
from ui import shell


def load_prefs(user, workspace_id):
    if "prefs" not in st.session_state:
        st.session_state.prefs = preferences.get(user["user_id"], workspace_id)
        st.session_state.debug_mode = bool(st.session_state.prefs.get("debug_mode"))
    return st.session_state.prefs


def prompt_builder_section(user, workspace_id):
    """AI Instructions (Prompt Builder): workspace-level style / format / personality / company context.
    Edit · Save · Reset · Preview, with validation and character limits. Data rules are not editable here."""
    import prompt_builder
    from backend import prompt_settings
    st.markdown("**AI Instructions (Prompt Builder)**")
    st.caption("Ye instructions jawab ka andaaz aur company ki terminology tay karti hain — data, columns, filters, dates aur numbers hamesha "
               "validated tools se aate hain; inhe koi instruction badal nahi sakti. Poore workspace (web + WhatsApp) par lagti hain.")
    if "ai_instructions" not in st.session_state:
        st.session_state.ai_instructions = prompt_settings.get(workspace_id)
    ins = st.session_state.ai_instructions
    for k in prompt_builder.FIELDS:
        limit = prompt_builder.MAX_CHARS[k]
        ins[k] = st.text_area(prompt_builder.LABELS[k], value=ins.get(k, ""), max_chars=limit, height=110, key=f"pb_{k}",
                              placeholder=prompt_builder.EXAMPLES[k], help=f"Max {limit} characters. Example: {prompt_builder.EXAMPLES[k][:120]}…")
        st.caption(f"{len(ins[k])}/{limit}")
    check = prompt_builder.validate(ins)
    for e in check["errors"]:
        st.error(e)
    for w in check["warnings"]:
        st.warning(w)
    c1, c2, c3 = st.columns(3)
    if c1.button("Save instructions", type="primary", disabled=not check["ok"], key="pb_save"):
        try:
            st.session_state.ai_instructions = prompt_settings.save(workspace_id, user["user_id"], ins)
            st.success("Saved — ab har jawab (web + WhatsApp) in instructions ke saath banega.")
        except (ValueError, PermissionError) as e:
            st.error(str(e))
    if c2.button("Reset to default", key="pb_reset"):
        st.session_state.ai_instructions = prompt_settings.reset(workspace_id, user["user_id"])
        for k in prompt_builder.FIELDS:
            st.session_state.pop(f"pb_{k}", None)
        st.rerun()
    if c3.button("Use examples", key="pb_examples", help="Example text bhar do; phir edit karke save karo"):
        st.session_state.ai_instructions = dict(prompt_builder.EXAMPLES)
        for k in prompt_builder.FIELDS:
            st.session_state.pop(f"pb_{k}", None)
        st.rerun()
    with st.expander("Preview: effective prompt structure", expanded=False):
        try:
            schemas = {s["id"]: {**(s.get("schema_snapshot") or {}), "name": s["name"]} for s in shell.REGISTRY.list(workspace_id, include_disabled=False) if s["status"] == "connected"}
        except Exception:
            schemas = None
        st.code(prompt_builder.preview(check["clean"], schemas), language="text")


def page():
    shell.styles()
    user, workspace_id = shell.current()
    prefs = load_prefs(user, workspace_id)
    st.markdown("### Settings")
    st.caption(f"Signed in as {user.get('name') or ''} · {user['email']}")

    st.markdown("**Answers**")
    prefs["number_style"] = st.selectbox("Number style", ["indian", "international"], index=["indian", "international"].index(prefs.get("number_style", "indian")),
                                         format_func={"indian": "Indian (lakh / crore)", "international": "International (million)"}.get)
    prefs["show_period"] = st.toggle("Show the date range under period answers", value=prefs.get("show_period", True))
    prefs["debug_mode"] = st.toggle("Debug mode (developers): show plans and tool trace", value=bool(prefs.get("debug_mode")))
    st.session_state.debug_mode = prefs["debug_mode"]

    st.markdown("**Text-to-Speech**")
    providers = available_providers()
    prefs["tts_enabled"] = st.toggle("Show the Listen control under answers", value=prefs.get("tts_enabled", True))
    prov = st.selectbox("Speech engine", list(providers), format_func=providers.get, index=list(providers).index(prefs.get("tts_provider", "browser")) if prefs.get("tts_provider", "browser") in providers else 0)
    prefs["tts_provider"] = prov
    if get_provider(prov).sends_data_externally:
        st.warning("This engine sends answer text to a third-party service.")
    prefs["tts_auto"] = st.toggle("Auto speak new answers", value=bool(prefs.get("tts_auto")))
    prefs["tts_lang"] = st.selectbox("Speech language", ["auto", "en", "hi", "hinglish"], index=["auto", "en", "hi", "hinglish"].index(prefs.get("tts_lang", "auto")),
                                     format_func={"auto": "Auto-detect", "en": "English", "hi": "Hindi", "hinglish": "Hinglish"}.get)
    if prov == "browser":
        voices = [v for v in browser_voices() if str(v.get("lang", "")).lower()[:2] in {"en", "hi"}]
        labels = {v["name"]: f'{v["name"]} ({v["lang"]})' for v in voices}
    else:
        labels = {n: n for n in get_provider(prov).list_voices()}
    opts = [""] + list(labels)
    prefs["tts_voice"] = st.selectbox("Voice", opts, index=opts.index(prefs.get("tts_voice", "")) if prefs.get("tts_voice", "") in opts else 0,
                                      format_func=lambda n: labels.get(n, n) if n else "Auto (match answer language)")
    prefs["tts_speed"] = st.slider("Speech speed", 0.5, 2.0, float(prefs.get("tts_speed", 1.0)), 0.1)
    prefs["tts_volume"] = st.slider("Volume", 0.0, 1.0, float(prefs.get("tts_volume", 1.0)), 0.1)

    st.markdown("**WhatsApp**")
    st.caption("Apna WhatsApp number is workspace se jodo: bot ko `/link <code>` bhejo. Uske baad WhatsApp par wahi sources aur sawal chalenge.")
    links = whatsapp_link.list_links(workspace_id)
    for l in links:
        c1, c2 = st.columns([5, 1])
        c1.write(f"• {'linked' if l['linked_at'] else 'pending code: **' + str(l['link_code']) + '**'}" + (f" · number ending {l['external_last4']}" if l["external_last4"] else ""))
        if c2.button("✕", key=f"ul_{l['id']}"):
            whatsapp_link.unlink(workspace_id, l["id"])
            st.rerun()
    if st.button("Generate link code"):
        code = whatsapp_link.create_code(workspace_id, user["user_id"])
        st.success(f"WhatsApp par bot ko bhejo:  `/link {code}`  (code 24 ghante valid hai)")

    prompt_builder_section(user, workspace_id)

    if os.getenv("APP_ALLOW_MODEL_OVERRIDE", "").lower() in ("1", "true", "yes"):
        st.markdown("**Model**")
        st.session_state.model_override = st.text_input("OpenRouter model", value=st.session_state.get("model_override") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini"))

    if st.button("Save settings", type="primary"):
        preferences.save(user["user_id"], workspace_id, prefs)
        st.success("Saved — ye settings har device par lagengi.")
    st.divider()
    if st.button("Logout"):
        shell.logout()
