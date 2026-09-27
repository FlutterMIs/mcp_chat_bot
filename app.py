import asyncio, os, re, uuid
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from mcp_client import MCPClient
from source_loader import read_file
from openrouter import OpenRouterAI
from memory import clear_memory
import analyst
from analyst import Conversation, LocalTools
from response import finalize
from tts_service import LANG_TAGS, available_providers, build_speech_text, get_provider
from tts_ui import browser_voices, speech_controls
from chart_ui import chart_menu, chartable, draw_selected_chart

load_dotenv(); st.set_page_config(page_title="MCP Universal Business Analyst",page_icon="🧠",layout="wide")
for k,v in {"messages":[],"last_plan":None,"recent_plans":[],"focus":None,"sources":{},"schemas":{},"autoplay_id":None,"analysis":None,"last_result":None,"last_cards":[],"pending_q":None}.items(): st.session_state.setdefault(k,v)

st.markdown("## 🧠 Business Analyst")
_src=[re.split(r"\s[–|-]\s",str(s.get("name") or sid))[0][:28] for sid,s in st.session_state.schemas.items()]
st.caption(("Connected: " + " · ".join(f"**{n}**" for n in _src)) if _src else "Koi data connected nahi — sidebar se Google Sheet, file, website ya database jodo.")

with st.sidebar:
    st.header("Configuration")
    # The .env key is used server-side and never sent to the browser; the box only overrides it for this session.
    _env_key=os.getenv("OPENROUTER_API_KEY","")
    api_key=st.text_input("OpenRouter API Key",value="",type="password",placeholder="loaded from .env ✅" if _env_key else "sk-or-…",help="Blank = key from .env") or _env_key
    model=st.text_input("OpenRouter Model",value=os.getenv("OPENROUTER_MODEL","openai/gpt-4.1-mini"))
    db_url=os.getenv("DATABASE_URL","sqlite:///demo.db")
    st.divider(); st.subheader("🌐 Web URL")
    web_url=st.text_input("Website / Web page URL",value="",placeholder="https://example.com/report")
    if st.button("➕ Add Web Source",width="stretch"):
        try:
            m=MCPClient(db_url); sid="web_"+uuid.uuid4().hex[:8]; parsed=__import__('source_loader').read_web(web_url.strip()); schema=m.register(sid,parsed); st.session_state.sources[sid]=parsed; st.session_state.schemas[sid]=schema; st.session_state.focus=sid; st.success(f"Added: {parsed.get('name','web page')}")
        except Exception as e: st.error(str(e))
    st.divider(); st.subheader("📊 Google Sheet")
    gs=st.text_input("Google Sheet URL",value=os.getenv("GOOGLE_SHEET_URL",""),placeholder="Paste spreadsheet URL")
    if st.button("🔌 Connect Google Sheet",width="stretch"):
        try:
            # One source per sheet URL, so several sheets can be connected side by side.
            sid="sheet_"+__import__('hashlib').sha1(gs.strip().encode()).hexdigest()[:8]
            m=MCPClient(db_url); schema=m.get_server().register_google_sheet(sid,gs.strip()); st.session_state.sources[sid]=m.get_server().sources[sid]; st.session_state.schemas[sid]=schema; st.session_state.focus=sid; st.success(f"Connected • {len(schema.get('sheets',[]))} tabs")
        except Exception as e: st.error(str(e))
    st.divider(); st.subheader("🗄️ Database")
    dbu=st.text_input("Database URL",value="",type="password",placeholder="postgresql://user:pass@host/db",
                      help="Any SQLAlchemy URL: postgresql://, mysql+pymysql://, sqlite:///file.db. Tables are read with SELECT only.")
    if st.button("🔌 Connect Database",width="stretch",disabled=not dbu.strip()):
        try:
            from source_loader import safe_db_name
            sid="db_"+__import__('hashlib').sha1(dbu.strip().encode()).hexdigest()[:8]
            m=MCPClient(db_url); schema=m.get_server().register_database(sid,dbu.strip()); st.session_state.sources[sid]=m.get_server().sources[sid]; st.session_state.schemas[sid]=schema; st.session_state.focus=sid
            st.success(f"Connected {safe_db_name(dbu.strip())} • {len(schema.get('sheets',[]))} tables")
        except Exception as e: st.error(str(e).replace(dbu.strip(),"<database url>")[:300])
    st.divider(); st.subheader("📁 Files")
    uploads=st.file_uploader("Upload CSV / XLSX / XLS / PDF / DOCX / PPTX / TXT / MD / JSON / HTML",accept_multiple_files=True,type=["csv","xlsx","xls","pdf","docx","pptx","txt","md","json","html","htm"])
    if uploads:
        for up in uploads:
            key="file_"+str(up.file_id if hasattr(up,'file_id') else up.name)
            if key not in st.session_state.sources:
                try:
                    parsed=read_file(up.name,up.getvalue()); m=MCPClient(db_url); schema=m.register(key,parsed); st.session_state.sources[key]=parsed; st.session_state.schemas[key]=schema; st.session_state.focus=key
                except Exception as e: st.error(f"{up.name}: {e}")
        st.success(f"Loaded {len(uploads)} file(s)")
    st.divider(); st.subheader("🧠 Self-learning memory")
    st.caption("Memory learns explicit mappings/rules and successful plans. It does not retrain the LLM.")
    if st.button("🧹 Clear learned memory",width="stretch"): clear_memory(); st.success("Memory cleared")
    st.toggle("Debug mode",value=False,key="debug_mode",help="Tool trace aur plan dikhao (sirf development ke liye)")
    st.divider(); st.subheader("🔊 Text-to-Speech")
    tts_providers=available_providers()
    tts_provider=st.selectbox("Speech engine",list(tts_providers),format_func=tts_providers.get,key="tts_provider")
    if get_provider(tts_provider).sends_data_externally: st.warning("This engine sends answer text to a third-party service.")
    tts_auto=st.toggle("Auto Speak",value=False,key="tts_auto")
    tts_lang=st.selectbox("Speech language",["auto","en","hi","hinglish"],format_func={"auto":"Auto-detect","en":"English","hi":"Hindi","hinglish":"Hinglish"}.get,key="tts_lang")
    tts_numbers=st.selectbox("Number style",["indian","international"],format_func={"indian":"Indian (lakh / crore)","international":"International (million)"}.get,key="tts_numbers")
    if tts_provider=="browser":
        voices=[v for v in browser_voices() if str(v.get("lang","")).lower()[:2] in {"en","hi"}]
        voices.sort(key=lambda v:(not v["lang"].lower().startswith("hi"),not v["lang"].lower().startswith("en-in"),v["lang"],v["name"]))
        voice_labels={v["name"]:f'{v["name"]} ({v["lang"]})' for v in voices}
    else:
        voice_labels={n:n for n in get_provider(tts_provider).list_voices()}
    tts_voice=st.selectbox("Voice",[""]+list(voice_labels),format_func=lambda n:voice_labels.get(n,n) if n else "Auto (match answer language)",key=f"tts_voice_{tts_provider}")
    tts_speed=st.slider("Speech speed",0.5,2.0,1.0,0.1,key="tts_speed")
    tts_volume=st.slider("Volume",0.0,1.0,1.0,0.1,key="tts_volume",disabled=tts_provider!="browser",help="For server-generated audio, use the audio player's volume.")
    st.divider(); st.subheader("Try asking")
    st.markdown("- **sales amount category wise**\n- **sales month wise**\n- **AMOUNT se total batao**\n- **data do details do**\n- **is website ka summary do**\n- **PDF me total revenue kitna hai?**")

# show sources
if st.session_state.schemas:
    with st.expander("🔎 Connected sources / real schema",expanded=False):
        for sid,schema in st.session_state.schemas.items():
            st.markdown(f"**{sid}** — {schema.get('name',schema.get('kind','source'))}")
            if schema.get('sheets'):
                for sh in schema['sheets']:
                    st.write(f"• {sh['name']} ({sh['row_count']} rows): " + ", ".join(c['name'] for c in sh['columns']))
            elif schema.get('columns'):
                st.write(", ".join(c['name'] for c in schema['columns']))

def _fmt_card(v, label):
    from analyst import _fmt
    try: return _fmt(float(v), label)
    except (TypeError, ValueError): return str(v)

def _xlsx_bytes(df):
    import io
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as w: df.to_excel(w,index=False,sheet_name="result")
    return buf.getvalue()

def render_assistant(m):
    m.setdefault('id',uuid.uuid4().hex[:12])
    if m.get('cards'):
        cols=st.columns(min(4,len(m['cards'])))
        for i,c in enumerate(m['cards']):
            with cols[i%len(cols)]: st.metric(c['label'], _fmt_card(c['value'], c['label']), help=c.get('note'))
    st.markdown(m['content'])
    if m.get('debug') and st.session_state.get('debug_mode'):
        with st.expander("Debug: trace & plan", expanded=False):
            for t in m['debug'].get('trace') or []: st.caption(t)
            if m['debug'].get('plan'): st.json(m['debug']['plan'])
    if m.get('videos'):
        for v in m['videos'][:3]:   # the rest are listed as links in the text
            st.video(v['url']); st.caption(v['title'])
    if m.get('images'):
        cols=st.columns(min(3,len(m['images'])))
        for i,img in enumerate(m['images']):
            with cols[i%len(cols)]: st.image(img['url'],caption=img['alt'],width="stretch")
    df=m.get('df')
    if df is not None and not df.empty and not (len(df)==1 and list(df.columns)==["value"]):   # a lone number is already in the text
        shown=df.rename(columns={"value":m['metric']}) if m.get('metric') and "value" in df.columns and m['metric'] not in df.columns else df
        if m.get('drillable') and m['drillable'] in shown.columns:
            st.caption("Kisi row par click karo — us period ka detail khulega.")
            ev=st.dataframe(shown,width="stretch",hide_index=True,on_select="rerun",selection_mode="single-row",key=f"tbl_{m['id']}")
            sel=(ev.selection.rows if ev and getattr(ev,'selection',None) else [])
            if sel and st.session_state.get(f"drilled_{m['id']}")!=sel[0]:
                st.session_state[f"drilled_{m['id']}"]=sel[0]
                st.session_state.pending_q=f"{shown.iloc[sel[0]][m['drillable']]} details"
                st.rerun()
        else:
            st.dataframe(shown,width="stretch",hide_index=True)
        if m.get('table_note'): st.caption(m['table_note'])
    if m.get('options'):
        pick=st.pills("Choose",m['options'],selection_mode="single",key=f"opt_{m['id']}",label_visibility="collapsed")
        if pick and st.session_state.get(f"picked_{m['id']}")!=pick:
            st.session_state[f"picked_{m['id']}"]=pick; st.session_state.pending_q=pick; st.rerun()
    for f in m.get('files') or []:
        st.download_button(f"📄 Download {f['name']}",f['bytes'],f['name'],f['mime'],key=f"file_{m['id']}_{f['name']}")
    if df is not None and not df.empty and not (len(df)==1 and list(df.columns)==["value"]):
        with st.container(horizontal=True):
            st.download_button('⬇️ CSV',df.to_csv(index=False).encode(),'report.csv','text/csv',key=f"dl_{m['id']}")
            st.download_button('⬇️ Excel',_xlsx_bytes(df),'report.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',key=f"dlx_{m['id']}")
            if st.button('📄 Report',key=f"rep_{m['id']}",help="Summary + data ka Excel report"): st.session_state.pending_q="report bana do"; st.rerun()
            if chartable(df): chart_menu(df,m['id'],m.get('chart'),m.get('metric'),auto=bool(m.get('chart')))
        draw_selected_chart(df,m['id'])
    # TTS is presentation-only: it reads the final answer, never plans/tool output, and never breaks the chat.
    try:
      with st.expander("🔊 Listen", expanded=bool(tts_auto and m['id']==st.session_state.autoplay_id)):
        rows=df.to_dict(orient='records') if df is not None and not df.empty else None
        text,lang=build_speech_text(m['content'],question=m.get('question',''),rows=rows,chart=m.get('chart'),metric=m.get('metric'),language=None if tts_lang=="auto" else tts_lang,number_style=tts_numbers)
        speech_controls(m['id'],text,LANG_TAGS.get(lang,LANG_TAGS['en']),provider=tts_provider,voice=tts_voice or None,speed=tts_speed,volume=tts_volume,autoplay=tts_auto and m['id']==st.session_state.autoplay_id,language=lang)
    except Exception:
        st.caption("Text-to-Speech is currently unavailable. You can continue using the text response.")

def add_assistant(content,question,**extra):
    m={'id':uuid.uuid4().hex[:12],'role':'assistant','content':content,'question':question,**extra}
    st.session_state.messages.append(m); st.session_state.autoplay_id=m['id']
    return m

def render_user(m):
    if m.get('audio'):
        st.audio(m['audio'],format="audio/wav")
        st.caption(":material/mic: Voice message — samjha gaya:")
    st.markdown(m['content'])

for m in st.session_state.messages:
    with st.chat_message(m['role']):
        if m['role']=='assistant': render_assistant(m)
        else: render_user(m)

def answer_question(q):
    """Web channel: the shared analyst brain, with this browser session's sources and context."""
    m=MCPClient(db_url)
    # Rehydrate current in-memory sources into this request's MCP server.
    for sid,src in st.session_state.sources.items(): m.register(sid,src)
    conv=Conversation(schemas=dict(st.session_state.schemas),last_plan=st.session_state.last_plan,recent_plans=list(st.session_state.recent_plans),focus=st.session_state.focus,
                      state=st.session_state.analysis,last_result=st.session_state.last_result,last_cards=list(st.session_state.last_cards or []),pending_choice=st.session_state.get("pending_choice"),
                      history=[{'role':x['role'],'content':x['content'],**({'media':[{'title':v['title'],'url':v['url']} for v in x['videos']] or None} if x.get('videos') else {}),
                               **({'media':[{'title':i['alt'],'url':i['url']} for i in x['images']]} if x.get('images') and not x.get('videos') else {})} for x in st.session_state.messages[:-1]])
    with st.status("Samajh raha hoon…",expanded=False) as status:
        steps={"route_state":"Data dhoondh raha hoon…","route_multi_metric":"Metrics nikaal raha hoon…","route_agent":"Analysis kar raha hoon…","ai_route":"Plan bana raha hoon…","agent_tool":"Calculate kar raha hoon…","ai_done":"Validate kar raha hoon…","complaint_verify":"Dobara check kar raha hoon…"}
        def _log(ev,**f):
            if ev in steps: status.update(label=steps[ev])
        r=analyst.answer(conv,q,api_key,model,LocalTools(m.get_server()),log=_log)
        status.update(label="Taiyar ✓",state="complete")
    st.session_state.last_plan=conv.last_plan; st.session_state.recent_plans=conv.recent_plans
    st.session_state.analysis=conv.state; st.session_state.last_result=conv.last_result; st.session_state.last_cards=conv.last_cards; st.session_state.pending_choice=conv.pending_choice
    fr=finalize(r,q,debug=bool(st.session_state.get('debug_mode')))
    extra={'shape':fr.shape}
    if fr.table is not None: extra.update({'df':fr.table,'chart':fr.chart,'metric':r.metric,'table_note':fr.table_note})
    if fr.metrics: extra['cards']=fr.metrics
    if fr.drilldown: extra['drillable']=fr.drilldown
    if fr.options: extra['options']=fr.options
    if fr.files: extra['files']=fr.files
    if fr.images: extra['images']=fr.images
    if fr.videos: extra['videos']=fr.videos
    if fr.debug: extra['debug']=fr.debug
    return add_assistant(fr.answer,q,**extra)

inp=st.chat_input("Kuch bhi normal language mein pucho… ya mic dabake bolo",accept_audio=True)
_pq=st.session_state.pending_q
if _pq and not inp:
    st.session_state.pending_q=None
    class _Click: text=_pq; audio=None
    inp=_Click()
if inp:
    q=(inp.text or "").strip(); audio=inp.audio.getvalue() if inp.audio else None
    if audio and not q:
        try:
            with st.spinner("Voice message samajh raha hoon…"):
                q=OpenRouterAI(api_key,model).transcribe(audio,model=os.getenv("OPENROUTER_STT_MODEL","google/gemini-2.5-flash"))
        except Exception as e:
            st.error(f"Voice message text mein nahi badal paaya: {e}"); q=""
    if q:
        user={'role':'user','content':q,**({'audio':audio} if audio else {})}
        st.session_state.messages.append(user)
        with st.chat_message('user'): render_user(user)
        try:
            with st.spinner("Data dekh raha hoon…"): m=answer_question(q)
        except Exception as e:
            m=add_assistant(f"❌ {e}",q)
        with st.chat_message('assistant'): render_assistant(m)
