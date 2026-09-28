"""Chat page: the conversation, one FinalResponse per assistant turn, actions instead of a dashboard."""
from __future__ import annotations

import os
import time
import uuid

import pandas as pd
import streamlit as st

import analyst
from analyst import LocalTools, _fmt
import memory
from backend import conversations, events
from chart_ui import chart_menu, chartable, default_chart, draw_selected_chart
from mcp_server import MCPServer
from response import display_table, finalize
from tts_service import LANG_TAGS, build_speech_text
from tts_ui import speech_controls
from ui import shell


def _prefs():
    return st.session_state.get("prefs") or {}


def _fmt_card(v, label):
    try:
        return _fmt(float(v), label)
    except (TypeError, ValueError):
        return str(v)


def _xlsx_bytes(df):
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="result")
    return buf.getvalue()


def render_assistant(m):
    """One assistant message from its stored FinalResponse (+ df when the result had rows)."""
    m.setdefault("id", uuid.uuid4().hex[:12])
    fr = m.get("final_response") or {}
    shape = fr.get("shape") or m.get("shape") or "text"
    st.markdown(m["content"])
    cards = fr.get("metrics") or m.get("cards") or []
    if cards and (shape == "scalar" or len(cards) >= 2):          # scalar: the one figure; compound / multi-measure: one card each
        cols = st.columns(min(4, len(cards)))
        for i, c in enumerate(cards):
            with cols[i % len(cols)]:
                st.metric(c["label"], _fmt_card(c["value"], c["label"]), help=c.get("note"))
    if fr.get("date_range") and shape != "text" and _prefs().get("show_period", True) and fr.get("period"):
        st.caption("Period: " + fr["date_range"].replace("→", "to"))
    if m.get("debug") and st.session_state.get("debug_mode"):
        with st.expander("Debug: trace & plan", expanded=False):
            for t in m["debug"].get("trace") or []:
                st.caption(t)
            if m["debug"].get("plan"):
                st.json(m["debug"]["plan"])
    for v in (fr.get("videos") or m.get("videos") or [])[:3]:
        st.video(v["url"])
        st.caption(v["title"])
    imgs = fr.get("images") or m.get("images") or []
    if imgs:
        cols = st.columns(min(3, len(imgs)))
        for i, img in enumerate(imgs):
            with cols[i % len(cols)]:
                st.image(img["url"], caption=img["alt"], width="stretch")
    df = m.get("df")
    chart = fr.get("chart") if fr else m.get("chart")
    metric = fr.get("metric") or m.get("metric")
    has_table = df is not None and not df.empty
    if has_table:
        shown = df.rename(columns={"value": metric}) if metric and "value" in df.columns and metric not in df.columns else df
        if chartable(shown):
            _seed_chart(shown, m["id"], chart, metric)
            draw_selected_chart(shown, m["id"])
        open_table = shape in ("ranking", "detail", "breakdown") or not chart
        with st.expander(f"Table · {len(shown):,} rows" if len(shown) > 1 else "Table", expanded=open_table):
            drill = fr.get("drilldown") or m.get("drillable")
            pretty = display_table(shown)
            if drill and drill in shown.columns:
                st.caption("Row par click karo — us period ka detail khulega.")
                ev = st.dataframe(pretty, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row", key=f"tbl_{m['id']}")
                sel = ev.selection.rows if ev and getattr(ev, "selection", None) else []
                if sel and st.session_state.get(f"drilled_{m['id']}") != sel[0]:
                    st.session_state[f"drilled_{m['id']}"] = sel[0]
                    st.session_state.pending_q = f"{shown.iloc[sel[0]][drill]} details"
                    st.rerun()
            else:
                st.dataframe(pretty, width="stretch", hide_index=True)
            if fr.get("table_note") or m.get("table_note"):
                st.caption(fr.get("table_note") or m.get("table_note"))
        with st.container(horizontal=True):
            from exports import MIME, ResultExport
            ex = ResultExport(title=str(metric or fr.get("source_info", {}).get("metric") or "Result"), question=m.get("question", ""), table=df, value=fr.get("value"),
                              metrics=cards, source_info=fr.get("source_info") or {}, answer=m["content"])
            st.download_button("⬇️ CSV", ex.to_csv(), "result.csv", MIME["csv"], key=f"dl_{m['id']}")
            st.download_button("⬇️ Excel", ex.to_xlsx(), "result.xlsx", MIME["xlsx"], key=f"dlx_{m['id']}")
            st.download_button("⬇️ PDF", ex.to_pdf(), "result.pdf", MIME["pdf"], key=f"dlp_{m['id']}")
            if st.button("📄 Report", key=f"rep_{m['id']}", help="Summary + data ka Excel report"):
                st.session_state.pending_q = "report bana do"
                st.rerun()
            if chartable(shown):
                chart_menu(shown, m["id"], chart, metric, auto=bool(chart))
    options = fr.get("options") or m.get("options") or []
    if options and m is st.session_state.messages[-1]:
        pick = st.pills("Choose", options, selection_mode="single", key=f"opt_{m['id']}", label_visibility="collapsed")
        if pick and st.session_state.get(f"picked_{m['id']}") != pick:
            st.session_state[f"picked_{m['id']}"] = pick
            st.session_state.pending_q = pick
            st.rerun()
    for f in m.get("files") or []:
        st.download_button(f"📄 Download {f['name']}", f["bytes"], f["name"], f["mime"], key=f"file_{m['id']}_{f['name']}")
    prefs = _prefs()
    if prefs.get("tts_enabled", True):
        try:
            with st.expander("🔊 Listen", expanded=bool(prefs.get("tts_auto") and m["id"] == st.session_state.get("autoplay_id"))):
                rows = df.to_dict(orient="records") if has_table else None
                lang_pref = prefs.get("tts_lang", "auto")
                text, lang = build_speech_text(m["content"], question=m.get("question", ""), rows=rows, chart=chart, metric=metric,
                                               language=None if lang_pref == "auto" else lang_pref, number_style=prefs.get("number_style", "indian"))
                speech_controls(m["id"], text, LANG_TAGS.get(lang, LANG_TAGS["en"]), provider=prefs.get("tts_provider", "browser"), voice=prefs.get("tts_voice") or None,
                                speed=float(prefs.get("tts_speed", 1.0)), volume=float(prefs.get("tts_volume", 1.0)),
                                autoplay=bool(prefs.get("tts_auto")) and m["id"] == st.session_state.get("autoplay_id"), language=lang)
        except Exception:
            st.caption("Text-to-Speech abhi available nahi hai.")


def _seed_chart(df, msg_id, chart, metric):
    """The chart is drawn above the table, the ⋮ menu below it: seed the menu's state first so both agree."""
    best = default_chart(df, chart, metric) if chart else None
    st.session_state.setdefault(f"chart_type_{msg_id}", best["type"] if best else "none")
    if best:
        st.session_state.setdefault(f"chart_x_{msg_id}", best["x"])
        st.session_state.setdefault(f"chart_y_{msg_id}", best["y"])


def render_user(m):
    if m.get("audio"):
        st.audio(m["audio"], format="audio/wav")
        st.caption(":material/mic: Voice message — samjha gaya:")
    st.markdown(m["content"])


def _load_messages(workspace_id, conversation_id):
    out = []
    for m in conversations.messages(workspace_id, conversation_id):
        item = {"id": m["id"], "role": m["role"], "content": m["content"], "final_response": m.get("final_response") or {}, "df": m.get("df"), "question": ""}
        out.append(item)
    for i, m in enumerate(out):
        if m["role"] == "assistant" and i > 0:
            m["question"] = out[i - 1]["content"]
    return out


def answer_question(user, workspace_id, q, audio=None):
    """The web channel: registry sources → tools; DB conversation → analyst → FinalResponse → saved + shown."""
    request_id = events.new_request_id()
    if st.session_state.get("conversation_id") is None:
        c = conversations.create(workspace_id, user["user_id"], channel="web")
        st.session_state.conversation_id = c["id"]
    cid = st.session_state.conversation_id
    events.bind(request_id=request_id, conversation_id=cid, workspace_id=workspace_id)
    memory.set_scope(workspace_id, user["user_id"])
    server = MCPServer(os.getenv("DATABASE_URL", "sqlite:///demo.db"))
    schemas, errors = shell.REGISTRY.ensure(server, workspace_id)
    conv = conversations.hydrate(workspace_id, cid, schemas)
    api_key = os.getenv("OPENROUTER_API_KEY", "") or st.session_state.get("api_key_override", "")
    model = st.session_state.get("model_override") or os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    steps = {"route_state": "Data dhoondh raha hoon…", "route_multi_metric": "Metrics nikaal raha hoon…", "route_agent": "Analysis kar raha hoon…", "ai_route": "Plan bana raha hoon…",
             "agent_tool": "Calculate kar raha hoon…", "ai_done": "Validate kar raha hoon…", "complaint_verify": "Dobara check kar raha hoon…"}
    t0 = time.time()
    with st.status("Samajh raha hoon…", expanded=False) as status:
        def _log(ev, **f):
            if ev in steps:
                status.update(label=steps[ev])
            events.log_event(ev, **{k: v for k, v in f.items() if k in ("kind", "status", "reason", "attempt", "error", "source", "sheet", "ms")})
        r = analyst.answer(conv, q, api_key, model, LocalTools(server), log=_log)
        status.update(label="Taiyar ✓", state="complete")
    fr = finalize(r, q, debug=bool(st.session_state.get("debug_mode")))
    if errors:
        names = {s["id"]: s["name"] for s in shell.REGISTRY.list(workspace_id)}
        fr.answer += "\n\n⚠️ " + "; ".join(f"*{names.get(sid, sid)}* refresh nahi hua (purana data use hua)" for sid in errors)
    conversations.save_turn(workspace_id, cid, q, r, fr, conv, request_id=request_id)
    events.log_event("answer", ms=(time.time() - t0) * 1000, shape=fr.shape, kind=fr.kind)
    m = {"id": uuid.uuid4().hex[:12], "role": "assistant", "content": fr.answer, "question": q, "df": fr.table, "files": fr.files,
         "final_response": {"shape": fr.shape, "kind": fr.kind, "metrics": fr.metrics, "chart": fr.chart, "table_note": fr.table_note, "drilldown": fr.drilldown,
                            "options": fr.options, "source_info": fr.source_info, "period": fr.period, "value": fr.value, "date_range": fr.date_range,
                            "images": fr.images, "videos": fr.videos, "metric": r.metric},
         "debug": fr.debug or None}
    st.session_state.messages.append(m)
    st.session_state.autoplay_id = m["id"]
    return m


def page():
    shell.styles()
    user, workspace_id = shell.current()
    if st.session_state.get("messages") is None:
        cid = st.session_state.get("conversation_id")
        st.session_state.messages = _load_messages(workspace_id, cid) if cid else []
    srcs = shell.REGISTRY.list(workspace_id, include_disabled=False)
    ok = [s for s in srcs if s["status"] == "connected"]
    title = "New chat"
    if st.session_state.get("conversation_id"):
        try:
            title = conversations.get(workspace_id, st.session_state.conversation_id)["title"]
        except Exception:
            shell.new_chat()
    st.markdown(f"### {title}")
    if not ok:
        st.info("Koi data source connected nahi hai. **Data Sources** page se Google Sheet, website, file ya database jodo — ek baar jodne ke baad har device par milega.")
    else:
        st.caption("Connected: " + " · ".join(f"**{s['name']}**" for s in ok[:4]) + (f" +{len(ok) - 4}" if len(ok) > 4 else ""))
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            (render_assistant if m["role"] == "assistant" else render_user)(m)
    inp = st.chat_input("Kuch bhi normal language mein pucho… ya mic dabake bolo", accept_audio=True)
    pq = st.session_state.get("pending_q")
    if pq and not inp:
        st.session_state.pending_q = None

        class _Click:
            text = pq
            audio = None
        inp = _Click()
    if not inp:
        return
    q = (inp.text or "").strip()
    audio = inp.audio.getvalue() if inp.audio else None
    if audio and not q:
        try:
            from openrouter import OpenRouterAI
            with st.spinner("Voice message samajh raha hoon…"):
                q = OpenRouterAI(os.getenv("OPENROUTER_API_KEY", ""), os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")).transcribe(audio, model=os.getenv("OPENROUTER_STT_MODEL", "google/gemini-2.5-flash"))
        except Exception as e:
            st.error(f"Voice message text mein nahi badal paaya: {e}")
            q = ""
    if not q:
        return
    um = {"id": uuid.uuid4().hex[:12], "role": "user", "content": q, **({"audio": audio} if audio else {})}
    st.session_state.messages.append(um)
    with st.chat_message("user"):
        render_user(um)
    try:
        with st.spinner("Data dekh raha hoon…"):
            m = answer_question(user, workspace_id, q, audio)
    except Exception as e:
        events.log_event("answer_failed", error=type(e).__name__)
        m = {"id": uuid.uuid4().hex[:12], "role": "assistant", "content": f"❌ {e}", "question": q, "final_response": {"shape": "text"}}
        st.session_state.messages.append(m)
    with st.chat_message("assistant"):
        render_assistant(m)
    st.rerun()
