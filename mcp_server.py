import difflib
import re
from urllib.parse import urlparse, parse_qs
import pandas as pd
from sqlalchemy import create_engine, inspect, text
from database import get_schema
from source_loader import column_info, normalize_columns, parse_dates, read_web


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def resolve_column(df, requested):
    if requested is None:
        return None
    cols = list(df.columns)
    req = str(requested).strip()
    exact = next((c for c in cols if str(c).strip() == req), None)
    if exact:
        return exact
    nreq = norm(req)
    case = [c for c in cols if norm(c) == nreq]
    if len(case) == 1:
        return case[0]
    aliases = {
        "sales amount": ["amount", "sales amount", "sale amount", "net amount", "total amount"],
        "sale amount": ["amount", "sales amount", "sale amount", "net amount", "total amount"],
        "sales": ["amount", "sales amount", "sale amount", "net sales", "revenue"],
        "revenue": ["amount", "sales amount", "sale amount", "net sales", "revenue"],
        "quantity": ["qty", "quantity", "alt qty", "alt_qty"],
        "qty": ["qty", "quantity"],
        "category": ["category", "item category", "item_category"],
    }
    candidates = aliases.get(nreq, [])
    hits = [c for c in cols if norm(c) in {norm(x) for x in candidates}]
    if len(hits) == 1:
        return hits[0]
    return None


FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "not_in"}


def resolve_value(series, value, column=None):
    """Map a user-typed value onto a real value in the column (case, spacing, small typos).
    Several equally close real values → AmbiguousValue (the bot asks); nothing close → None."""
    from entities import AmbiguousValue, match_entity
    values = [v for v in series.dropna().astype(str).str.strip().unique()]
    want = str(value).strip()
    m = match_entity(values, want)
    if m["status"] in ("exact", "single"):
        return m["value"]
    if m["status"] == "ambiguous":
        raise AmbiguousValue(column or series.name, want, m["candidates"])
    starts = [v for v in values if norm(v).startswith(norm(want)) or norm(want).startswith(norm(v))]
    if len(starts) == 1 and len(norm(want)) >= 3:
        return starts[0]
    return None


def apply_filters(df, filters):
    """Apply planner filters. Returns (rows, notes). Unknown values raise with the closest real values."""
    work, notes = df, []
    if filters and not isinstance(filters, list):
        raise ValueError('filters must be a list of {"column", "op", "value"} objects')
    for f in filters or []:
        if not isinstance(f, dict) or "column" not in f:
            raise ValueError(f'Bad filter {str(f)[:80]!r}: each filter must be an object like {{"column": "NAME", "op": "eq", "value": "x"}}')
        c = resolve_column(work, f.get("column"))
        if not c:
            raise ValueError(f'Filter column "{f.get("column")}" not found. Available columns: {", ".join(map(str, df.columns))}')
        op = str(f.get("op") or "eq").lower()
        if op not in FILTER_OPS:
            raise ValueError(f"Unsupported filter op: {op}")
        raw = f.get("value")
        col = work[c]
        if op in {"gt", "gte", "lt", "lte"}:
            nums = pd.to_numeric(col, errors="coerce")
            if nums.notna().any():
                target = pd.to_numeric(pd.Series([raw]), errors="coerce").iloc[0]
                if pd.isna(target):
                    raise ValueError(f'Filter on "{c}" needs a number, got "{raw}".')
            else:
                nums, target = parse_dates(col), pd.Timestamp(str(raw))
            mask = {"gt": nums > target, "gte": nums >= target, "lt": nums < target, "lte": nums <= target}[op]
        elif op == "contains":
            exact = [v for v in col.dropna().astype(str).str.strip().unique() if norm(v) == norm(raw)]
            if exact:
                # The user typed a full, real name ("DILIP JI"): match it exactly, so look-alikes such as
                # "SAMPLE ( DILIP JI )" are not summed in. Partial words ("telescopic") stay a contains match.
                mask = col.astype(str).map(norm) == norm(raw)
            else:
                mask = col.astype(str).str.contains(str(raw), case=False, regex=False, na=False)
                if not mask.any():
                    # "Pranjli ji" typed for "Pranjali Ji": a clear closest real value is used (and reported); a tie is asked.
                    fixed = resolve_value(col, raw, column=c)
                    if fixed is None:
                        raise ValueError(f'No rows contain "{raw}" in column "{c}".')
                    notes.append(f'"{raw}" → "{fixed}" ({c})')
                    mask = col.astype(str).map(norm) == norm(fixed)
        else:
            wanted = raw if isinstance(raw, list) else [raw]
            real = []
            for w in wanted:
                r = resolve_value(col, w, column=c)
                if r is None:
                    sample = ", ".join(col.dropna().astype(str).str.strip().value_counts().index[:15])
                    raise ValueError(f'Value "{w}" not found in column "{c}". Real values include: {sample}')
                if norm(r) != norm(w):
                    notes.append(f'"{w}" → "{r}" ({c})')
                real.append(norm(r))
            hit = col.astype(str).map(norm).isin(real)
            mask = ~hit if op in {"ne", "not_in"} else hit
        work = work[mask]
    return work, notes


def citations(hits):
    """Deduplicated [{"source", "page", "section"}] of retrieved chunks — the only citations an answer may carry."""
    out, seen = [], set()
    top = max((h.get("score") or 0) for h in hits) if hits else 0
    for h in hits or []:
        if top and (h.get("score") or 0) < 0.6 * top:
            continue                                  # weak neighbours are context for the model, not a citation
        key = (h.get("source"), h.get("page"), h.get("section"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": h.get("source"), "page": h.get("page"), "section": h.get("section")})
    return out


def citation_footer(cites):
    """'📄 Source: Return_Policy.pdf, Page 4 · Return_Policy.pdf, Page 5'. Empty when there is nothing to cite."""
    parts = []
    for c in cites or []:
        s = str(c.get("source") or "document")
        if c.get("page"):
            s += f", Page {c['page']}"
        elif c.get("section"):
            s += f" — {c['section']}"
        if s not in parts:
            parts.append(s)
    return ("📄 Source: " + " · ".join(parts[:4])) if parts else ""


class MCPServer:
    def __init__(self, database_url="sqlite:///demo.db"):
        self.database_url = database_url
        self.web_cache = {}
        self.sources = {}

    # ---------- common ----------
    @staticmethod
    def _column_info(df):
        return column_info(df)

    def register_file(self, source_id, parsed):
        self.sources[source_id] = parsed
        try:
            import rag
            rag.index_source(source_id, parsed)          # no-op unless RAG_ENABLED and the source has text
        except Exception:
            pass                                          # the knowledge layer never blocks a source from connecting
        return self.source_schema(source_id)

    def source_schema(self, source_id):
        if source_id not in self.sources:
            raise ValueError(f"Source not connected: {source_id}")
        src = self.sources[source_id]
        if src["kind"] == "workbook":
            return {"source_id": source_id, "kind": "workbook", "name": src["name"], "sheets": [{"name": n, "row_count": len(df), "columns": column_info(df)} for n, df in src["tabs"].items()]}
        if src["kind"] == "table":
            return {"source_id": source_id, "kind": "table", "name": src["name"], "row_count": len(src["df"]), "columns": column_info(src["df"])}
        tables = [{"name": n, "row_count": len(df), "columns": column_info(df)} for n, df in src.get("tables", {}).items()]
        # Web/document tables are queryable like workbook tabs (sheet_name = table name).
        return {"source_id": source_id, "kind": src["kind"], "name": src.get("name"), "url": src.get("url"), "text_length": len(src.get("text", "")),
                "tables": tables, "sheets": tables, "image_count": len(src.get("images") or []), "video_count": len(src.get("videos") or [])}

    # ---------- DB ----------
    def get_database_schema(self):
        return {"schema": get_schema(self.database_url), "source": "database"}

    def aggregate_data(self, table, metric, aggregation="sum", group_by=None, filters=None, limit=500, date_column=None, date_grain=None, date_from=None, date_to=None):
        engine = create_engine(self.database_url, future=True)
        insp = inspect(engine)
        tables = insp.get_table_names()
        if table not in tables:
            raise ValueError(f"Database table not found: {table}")
        columns = {c["name"] for c in insp.get_columns(table)}
        if metric not in columns:
            raise ValueError(f"Database metric column not found: {metric}")
        group_by = group_by or []
        for c in group_by:
            if c not in columns: raise ValueError(f"Database group column not found: {c}")
        if date_column and date_column not in columns: raise ValueError(f"Database date column not found: {date_column}")
        params = {}
        where=[]
        for i, f in enumerate(filters or []):
            c=f.get("column"); v=f.get("value")
            if c not in columns: raise ValueError(f"Database filter column not found: {c}")
            key=f"f{i}"; where.append(f'"{c}" = :{key}'); params[key]=v
        if date_from and date_column:
            where.append(f'"{date_column}" >= :date_from'); params["date_from"]=date_from
        if date_to and date_column:
            where.append(f'"{date_column}" <= :date_to'); params["date_to"]=date_to
        select=[]; group=[]
        if date_column and date_grain in {"day","month","year"}:
            fmt={"day":"%Y-%m-%d","month":"%Y-%m","year":"%Y"}[date_grain]
            select.append(f"strftime('{fmt}', \"{date_column}\") AS period"); group.append("period")
        for c in group_by: select.append(f'"{c}"'); group.append(f'"{c}"')
        agg=aggregation.lower()
        if agg not in {"sum","avg","count","min","max"}: raise ValueError("Invalid aggregation")
        expr="COUNT(*)" if agg=="count" else f"{agg.upper()}(\"{metric}\")"
        select.append(f"{expr} AS value")
        sql=f'SELECT {", ".join(select)} FROM "{table}"'
        if where: sql += " WHERE " + " AND ".join(where)
        if group: sql += " GROUP BY " + ", ".join(group)
        if group: sql += " ORDER BY " + ", ".join(group)
        sql += " LIMIT :row_limit"; params["row_limit"]=int(limit)
        with engine.connect() as conn: rows=conn.execute(text(sql),params).mappings().all()
        return {"rows":[dict(r) for r in rows],"count":len(rows),"source":"database"}

    # ---------- Google ----------
    @staticmethod
    def _sheet_id(url):
        m=re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)",url or "")
        if not m: raise ValueError("Invalid Google Sheet URL")
        return m.group(1)

    def load_google_workbook(self, url):
        import io, requests
        sid=self._sheet_id(url)
        export=f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx"
        r=requests.get(export,timeout=60)
        r.raise_for_status()
        if not r.content.startswith(b"PK"):
            raise ValueError("Google Sheet export failed. Make sure it is public/viewable by link.")
        xls=pd.ExcelFile(io.BytesIO(r.content),engine="openpyxl")
        tabs={name:normalize_columns(pd.read_excel(xls,sheet_name=name,engine="openpyxl")) for name in xls.sheet_names}
        return tabs

    def register_google_sheet(self, source_id, url):
        tabs=self.load_google_workbook(url)
        parsed={"kind":"workbook","name":source_id,"url":url,"tabs":tabs}
        return self.register_file(source_id,parsed)

    def aggregate_dataframe(self, df, metric, aggregation="sum", group_by=None, filters=None, limit=500, date_column=None, date_grain=None, date_from=None, date_to=None, sort=None, top_n=None):
        metric_col=resolve_column(df,metric)
        if not metric_col: raise ValueError(f'Metric column "{metric}" not found. Available columns: {", ".join(map(str,df.columns))}')
        groups=[]
        grains={"day":"day","date":"day","month":"month","year":"year"}
        for g in group_by or []:
            c=resolve_column(df,g)
            # Planner sometimes groups by a virtual "MONTH"/"YEAR" column: treat it as the date grain.
            if not c and date_column and str(g).strip().lower() in grains:
                date_grain=date_grain or grains[str(g).strip().lower()]; continue
            if not c: raise ValueError(f'Group column "{g}" not found. Available columns: {", ".join(map(str,df.columns))}')
            groups.append(c)
        date_col=resolve_column(df,date_column) if date_column else None
        if date_column and not date_col: raise ValueError(f'Date column "{date_column}" not found.')
        work,notes=apply_filters(df,filters)
        work=work.copy()
        if date_col:
            work["__date__"]=parse_dates(work[date_col])
            if date_from: work=work[work["__date__"]>=pd.Timestamp(date_from)]
            if date_to: work=work[work["__date__"]<=pd.Timestamp(date_to)+pd.Timedelta(days=1)-pd.Timedelta(microseconds=1)]
        agg=(aggregation or "sum").lower().replace(" ","_")
        if agg in {"distinct","nunique","count_unique","unique"}: agg="count_distinct"
        if agg in {"count","count_distinct"}:
            # Counting works on any column (item names are text): count filled-in cells, not numbers.
            txt=work[metric_col].astype(str).str.strip()
            work["__metric__"]=work[metric_col].where(work[metric_col].notna() & (txt!="") & (txt.str.lower()!="nan"))
        else:
            work["__metric__"]=pd.to_numeric(work[metric_col],errors="coerce")
        keys=[]
        if date_col and date_grain in {"day","month","year"}:
            if date_grain=="day": work["period"]=work["__date__"].dt.strftime("%Y-%m-%d")
            elif date_grain=="month": work["period"]=work["__date__"].dt.strftime("%Y-%m")
            else: work["period"]=work["__date__"].dt.strftime("%Y")
            keys.append("period")
        # Grouping by the raw date as well as its period would split every month back into single days.
        keys += [g for g in groups if not (keys and g==date_col)]
        if agg not in {"sum","avg","count","count_distinct","min","max"}: raise ValueError("Invalid aggregation (sum, avg, count, count_distinct, min, max)")
        if agg=="count_distinct":
            grouped=work.groupby(keys,dropna=False)["__metric__"].nunique() if keys else pd.Series({"value":int(work["__metric__"].nunique())})
        elif agg=="count":
            grouped=work.groupby(keys,dropna=False)["__metric__"].count() if keys else pd.Series({"value":int(work["__metric__"].count())})
        else:
            func={"sum":"sum","avg":"mean","min":"min","max":"max"}[agg]
            grouped=getattr(work.groupby(keys,dropna=False)["__metric__"],func)() if keys else pd.Series({"value":getattr(work["__metric__"],func)()})
        if keys:
            out=grouped.reset_index(name="value")
            if top_n and sort not in {"asc","desc"}: sort="desc"   # "top N" means the largest N, never the first N alphabetically
            if "period" in out and not top_n: out=out.sort_values(["period"]+[k for k in keys if k!="period"])   # time series stay chronological
            elif sort in {"asc","desc"}: out=out.sort_values("value",ascending=sort=="asc")
            groups_total=len(out)
            # Grand total across ALL groups, before top_n cuts the list (only meaningful for sum/count).
            grand=float(pd.to_numeric(out["value"],errors="coerce").sum()) if agg in {"sum","count"} else None
            if top_n: out=out.head(int(top_n))
        else:
            out=pd.DataFrame([{"value":grouped.iloc[0]}]); groups_total=1; grand=None
        out=out.head(int(limit or 500)).where(pd.notna(out),None)
        res={"rows":out.to_dict(orient="records"),"count":len(out),"source":"tabular","metric":metric_col,"aggregation":agg,"rows_matched":int(len(work)),"resolved_filters":notes}
        if keys and grand is not None:
            res["grand_total_all_groups"]=round(grand,2); res["groups_total"]=groups_total
        return res

    def query_dataframe(self, df, columns=None, filters=None, limit=500, sort_by=None, sort=None):
        work,notes=apply_filters(df,filters)
        total=len(work)
        if sort_by:
            sc=resolve_column(work,sort_by)
            if not sc: raise ValueError(f'Sort column "{sort_by}" not found. Available columns: {", ".join(map(str,work.columns))}')
            key=pd.to_numeric(work[sc],errors="coerce")
            work=work.assign(__k__=key if key.notna().any() else work[sc].astype(str)).sort_values("__k__",ascending=sort=="asc").drop(columns="__k__")
        if columns:
            resolved=[]
            for c in columns:
                rc=resolve_column(work,c)
                if not rc: raise ValueError(f'Column "{c}" not found. Available columns: {", ".join(map(str,work.columns))}')
                resolved.append(rc)
            work=work[resolved]
        head=work.head(int(limit or 500))
        return {"rows":head.astype(object).where(pd.notna(head),None).to_dict(orient="records"),"count":len(head),"rows_matched":total,"source":"tabular","resolved_filters":notes}

    def distinct_values(self, df, column, limit=50):
        c=resolve_column(df,column)
        if not c: raise ValueError(f'Column "{column}" not found. Available columns: {", ".join(map(str,df.columns))}')
        counts=df[c].dropna().astype(str).str.strip().value_counts()
        return {"column":c,"distinct_count":int(len(counts)),"values":[{"value":v,"rows":int(n)} for v,n in counts.head(int(limit)).items()]}

    # ---------- web/document ----------
    def register_database(self, source_id, url, max_rows=200000):
        from source_loader import read_database
        parsed=read_database(url,max_rows); self.sources[source_id]=parsed; return self.source_schema(source_id)

    def register_web(self, source_id, url):
        parsed=read_web(url); parsed.setdefault("name",url); self.sources[source_id]=parsed; return self.source_schema(source_id)

    STOPWORDS={"kya","hai","hain","iske","iska","iski","isme","isse","ye","yeh","batao","bta","btao","bata","kaise","kitna","kitne","kitni","mein","me","ka","ki","ke",
               "aur","bhi","the","and","what","is","are","this","that","about","tell","please","bro","bhai","do","de","dijiye","hota","hoti","wala","wali"}

    def search_text(self, source_id, query, max_chars=18000):
        """Text for answering a question about a page/document. Small sources go in whole (keyword search misses
        paraphrases, especially Hinglish questions about English pages); big ones are ranked by keyword hits."""
        src=self.sources[source_id]
        text=src.get("text","")
        base={"source":src.get("kind"),"name":src.get("name"),"url":src.get("url")}
        import rag
        if rag.enabled() and rag.has_index(source_id):
            # Hybrid RAG: only the relevant chunks (with source/page/section/score), never the whole document.
            hits=rag.search(source_id,query)
            if not hits:
                return base|{"text":"","chunks":[],"no_match":True}
            joined="\n\n".join(f"[{h['source']}" + (f", page {h['page']}" if h.get("page") else "") + (f", {h['section']}" if h.get("section") else "") + f"]\n{h['text']}" for h in hits)
            return base|{"text":joined[:max_chars],"chunks":[{k:h[k] for k in ("chunk_id","text","source","page","section","score")} for h in hits],
                         "citations":citations(hits)}
        if len(text)<=max_chars:
            return base|{"text":text}
        terms=[t for t in re.findall(r"[a-zA-Z0-9]{3,}",query.lower()) if t not in self.STOPWORDS]
        lines=[x.strip() for x in text.splitlines() if x.strip()]
        scored=sorted(((sum(line.lower().count(t) for t in terms),i,line) for i,line in enumerate(lines)),key=lambda x:(-x[0],x[1]))
        hits=[i for sc,i,_ in scored if sc][:120]
        keep=sorted(set(range(min(40,len(lines))))|{j for i in hits for j in (i-1,i,i+1) if 0<=j<len(lines)})   # page intro + hits with neighbours
        return base|{"text":"\n".join(lines[i] for i in keep)[:max_chars]}

    IMAGE_SYNONYMS={"report":["dashboard","report","analytics","performance","heatmap","call log","call-log"],"dashboard":["dashboard"],"app":["app"],
                    "whatsapp":["whatsapp"],"logo":["logos/"],"client":["logos/"],"customer":["customer","logos/"]}

    def find_images(self, source_id, query, limit=6):
        """Images of a web page whose alt text / file name matches the question (report screenshots, app screens...)."""
        src=self.sources.get(source_id) or {}
        imgs=src.get("images") or []
        if not imgs: return {"images":[],"total":0,"name":src.get("name")}
        terms=[t for t in re.findall(r"[a-z0-9]{3,}",query.lower()) if t not in self.STOPWORDS|{"image","images","photo","photos","pic","pics","picture","dikhao","dikha","bhejo","send","show"}]
        words={w for t in terms for w in self.IMAGE_SYNONYMS.get(t.rstrip("s"),[t])}
        def score(i):
            hay=(i["alt"]+" "+i["url"]).lower()
            return sum(w in hay for w in words)
        if "logos/" not in words:   # client logos only when logos/clients are asked for
            imgs=[i for i in imgs if "logo" not in i["url"].lower()] or imgs
        ranked=[i for i in sorted(imgs,key=score,reverse=True) if score(i)>0] if words else []
        if not ranked:   # generic "images dikhao": skip client logos
            ranked=[i for i in imgs if "logo" not in i["url"].lower()] or imgs
        return {"images":ranked[:int(limit)],"total":len(ranked),"name":src.get("name")}

    def find_videos(self, source_id, query="", limit=8):
        """Videos embedded/linked on a web page (YouTube, mp4 reels) that match the question; all of them if none match."""
        src=self.sources.get(source_id) or {}
        vids=src.get("videos") or []
        terms=[t for t in re.findall(r"[a-z0-9]{3,}",(query or "").lower()) if t not in self.STOPWORDS|{"video","videos","tutorial","tutorials","demo","share","link","links","bhejo","dikhao","karo","kro"}]
        hit=[v for v in vids if any(t in (v["title"]+" "+v["url"]).lower() for t in terms)] if terms else []
        chosen=hit or vids
        return {"videos":chosen[:int(limit)],"total":len(chosen),"name":src.get("name")}

    def _table(self, source_id, sheet_name=None):
        if source_id not in self.sources: raise ValueError(f"Source not connected: {source_id}")
        src=self.sources[source_id]
        if src["kind"]=="workbook":
            if sheet_name not in src["tabs"]: raise ValueError(f"Sheet/tab not found: {sheet_name}. Tabs: {', '.join(src['tabs'])}")
            return src["tabs"][sheet_name]
        if src.get("tables"):
            if sheet_name not in src["tables"]: raise ValueError(f"Table not found: {sheet_name}. Tables: {', '.join(src['tables'])}")
            return src["tables"][sheet_name]
        if "df" not in src: raise ValueError(f"Source {source_id} has no table; ask about its text instead.")
        return src["df"]

    def find_value(self, source_id, value, limit=5):
        """Where does a filter value actually live? [(sheet, column)] across every tab of the source."""
        src=self.sources.get(source_id) or {}
        tabs=src.get("tabs") or src.get("tables") or ({"": src["df"]} if "df" in src else {})
        want=norm(value); hits=[]
        for sheet,df in tabs.items():
            for c in df.columns:
                s=df[c].dropna().astype(str)
                if s.map(norm).eq(want).any() or (len(want)>=4 and s.str.contains(str(value),case=False,regex=False).any()):
                    hits.append((sheet,str(c)))
                    if len(hits)>=limit: return hits
        return hits

    def call_tool(self,name,args):
        try:
            return self._call_tool(name,args)
        except ValueError as e:
            from entities import AmbiguousValue
            if isinstance(e, AmbiguousValue):
                raise                                   # the analyst turns this into a choice for the user
            # A filter value missing from the chosen sheet: say where it does exist, so the planner can switch.
            msg=str(e)
            if name in ("aggregate_source","query_source") and ("not found in column" in msg or "No rows contain" in msg or "Filter column" in msg):
                bad_cols={str(f.get("column")) for f in args.get("filters") or []}
                vals=[f.get("value") for f in args.get("filters") or [] if isinstance(f.get("value"),str)]
                found=[f"sheet {sh or '-'} → column {col}" for v in vals for sh,col in self.find_value(args["source_id"],v) if col not in bad_cols or sh!=args.get("sheet_name")]
                if found: msg+=f" That value exists in: {', '.join(found)}. Filter on that column instead."
            raise ValueError(msg) from e

    def _call_tool(self,name,args):
        if name=="get_database_schema": return self.get_database_schema()
        if name=="aggregate_data": return self.aggregate_data(**args)
        if name=="register_google_sheet": return self.register_google_sheet(args["source_id"],args["url"])
        if name=="source_schema": return self.source_schema(args["source_id"])
        if name=="aggregate_source":
            sheet=args.get("sheet_name")
            df=self._table(args["source_id"],sheet)
            return self.aggregate_dataframe(df,**{k:args.get(k) for k in ["metric","aggregation","group_by","filters","limit","date_column","date_grain","date_from","date_to","sort","top_n"]}) | {"sheet_name":sheet}
        if name=="query_source":
            df=self._table(args["source_id"],args.get("sheet_name"))
            return self.query_dataframe(df,args.get("columns"),args.get("filters"),args.get("limit",500),args.get("sort_by"),args.get("sort")) | {"sheet_name":args.get("sheet_name")}
        if name=="distinct_values":
            return self.distinct_values(self._table(args["source_id"],args.get("sheet_name")),args["column"],args.get("limit",50))
        if name=="search_source": return self.search_text(args["source_id"],args["query"],args.get("max_chars",18000))
        if name=="load_file":
            from source_loader import check_allowed_path, read_file
            p=check_allowed_path(args["path"]); return self.register_file(args["source_id"],read_file(p.name,p.read_bytes()))
        if name=="register_web": return self.register_web(args["source_id"],args["url"])
        if name=="find_images": return self.find_images(args["source_id"],args.get("query",""),args.get("limit",6))
        if name=="find_videos": return self.find_videos(args["source_id"],args.get("query",""),args.get("limit",8))
        if name=="register_database": return self.register_database(args["source_id"],args["url"],args.get("max_rows",200000))
        raise ValueError(f"Unknown MCP tool: {name}")
