"""Strict, local-cacheable advisory TypeSafe decision contract."""
from __future__ import annotations
import hashlib, json, math, os, sqlite3, time
from pathlib import Path

MAX_BYTES, TTL, CAP = 32768, 300, 128

def _dump(value): return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
def _out(model=None, engine=None, reason=None): return {"status":"unassessed","reason_codes":[reason] if reason else [],"answers":{},"model":model,"usage":{},"latency_ms":0,"evaluations":0,"cache_hit":False,"engine_sha256":engine}
def _questions(qs):
    if not isinstance(qs,dict) or not 1<=len(qs)<=32: raise ValueError("questions")
    for ident,q in qs.items():
        if not isinstance(ident,str) or not ident or len(ident)>256 or not isinstance(q,dict): raise ValueError("question")
        if q.get("type") not in ("choice","noul","score") or not isinstance(q.get("instructions"),(str,dict,list)) or not q["instructions"]: raise ValueError("question")
        if q["type"]=="choice" and (not isinstance(q.get("criteria"),dict) or not 2<=len(q["criteria"])<=255 or not all(isinstance(k,str) and 1<=len(k)<=256 and (v is None or isinstance(v,(str,dict,list))) and v != "" for k,v in q["criteria"].items())): raise ValueError("criteria")
        if q["type"]=="noul" and ("criteria" in q and (not isinstance(q["criteria"],dict) or set(q["criteria"]) != {"true","false"} or not all(isinstance(v,str) and v for v in q["criteria"].values()))): raise ValueError("criteria")
        if q["type"]=="score" and (not isinstance(q.get("criteria"),list) or not 2<=len(q["criteria"])<=10 or not all(isinstance(x,str) and x for x in q["criteria"])): raise ValueError("criteria")
        if set(q)-{"type","instructions","criteria"}: raise ValueError("question_keys")
def _probs(p):
    if not isinstance(p,dict) or not p or not all(isinstance(k,str) and type(v) in (int,float) and math.isfinite(v) and 0<=v<=1 for k,v in p.items()) or abs(sum(p.values())-1)>.02: raise ValueError("probabilities")
def _answer(q,a):
    if not isinstance(a,dict): raise ValueError("answer")
    typ=q["type"]
    if typ=="choice":
        if set(a)!={"type","choice","probabilities","confidence"} or a["type"]!="choice" or not isinstance(a["choice"],str) or a["choice"] not in q["criteria"]: raise ValueError("choice")
        _probs(a["probabilities"])
        if set(a["probabilities"])!=set(q["criteria"]) or a["probabilities"][a["choice"]]+.001<max(a["probabilities"].values()): raise ValueError("choice")
    elif typ=="noul":
        if set(a)!={"type","noul"} or a["type"]!="noul" or type(a["noul"]) not in (int,float) or not math.isfinite(a["noul"]) or not 0<=a["noul"]<=1: raise ValueError("noul")
    else:
        if set(a)!={"type","score","probabilities","legend","confidence"} or a["type"]!="score" or type(a["score"]) not in (int,float) or not math.isfinite(a["score"]): raise ValueError("score")
        _probs(a["probabilities"]); legend={str(i):x for i,x in enumerate(q["criteria"])}
        # Provider scores use unrounded probabilities; validate their range, not exact reconstruction.
        if a["legend"]!=legend or set(a["probabilities"])!=set(legend) or not 0<=a["score"]<=len(q["criteria"])-1: raise ValueError("score")
    if "confidence" in a and (type(a["confidence"]) not in (int,float) or not math.isfinite(a["confidence"]) or not 0<=a["confidence"]<=1): raise ValueError("confidence")
def _db(directory):
    d=Path(directory).expanduser(); d.mkdir(parents=True,exist_ok=True,mode=0o700); p=d/"state.sqlite"; created=not p.exists(); con=sqlite3.connect(p)
    if created and os.name != "nt": os.chmod(p,0o600)
    con.execute("CREATE TABLE IF NOT EXISTS backend_decision_cache (k TEXT PRIMARY KEY,value BLOB,created REAL)"); con.execute("CREATE TABLE IF NOT EXISTS backend_decision_metrics (evaluations INTEGER,hits INTEGER,input_tokens INTEGER,output_tokens INTEGER,errors INTEGER)")
    if not con.execute("SELECT 1 FROM backend_decision_metrics LIMIT 1").fetchone(): con.execute("INSERT INTO backend_decision_metrics VALUES (0,0,0,0,0)")
    con.execute("DELETE FROM backend_decision_cache WHERE created<?",(time.time()-TTL,)); con.execute("DELETE FROM backend_decision_cache WHERE k IN (SELECT k FROM backend_decision_cache ORDER BY created DESC LIMIT -1 OFFSET ?)",(CAP,)); con.commit(); return con
def stats(directory):
    con=_db(directory)
    try: return dict(zip(("evaluations","cache_hits","input_tokens","output_tokens","errors"),con.execute("SELECT evaluations,hits,input_tokens,output_tokens,errors FROM backend_decision_metrics").fetchone()))
    finally: con.close()
def evaluate(payload, *, request, redact, directory, enabled, model, engine_sha256):
    out=_out(model,engine_sha256)
    if not enabled: out["reason_codes"]=["cloud_disabled"]; return out
    con=None
    try:
        if not isinstance(payload,dict) or not isinstance(payload.get("state"),(str,dict,list)): raise ValueError("payload")
        if set(payload)-{"state","questions","cache"}: raise ValueError("payload_keys")
        _questions(payload.get("questions")); desc=payload.get("cache")
        if desc is not None and (not isinstance(desc,dict) or set(desc)-{"namespace","generation"} or not isinstance(desc.get("namespace"),str) or not desc["namespace"] or len(desc["namespace"])>256 or not isinstance(desc.get("generation"),str) or not desc["generation"] or len(desc["generation"])>256): raise ValueError("cache")
        state,qs=redact(payload["state"]),redact(payload["questions"]); _questions(qs); req={"state":state,"questions":qs,"model":model}
        if len(_dump(req))>MAX_BYTES: raise ValueError("request_bound")
        changed = state != payload["state"] or qs != payload["questions"] or redact(desc) != desc
        con = _db(directory)
        key = None
        if desc and not changed:
            key=hashlib.sha256(_dump({"request":req,"engine":engine_sha256,"namespace":desc["namespace"],"generation":desc["generation"]})).hexdigest(); row=con.execute("SELECT value,created FROM backend_decision_cache WHERE k=?",(key,)).fetchone()
            if row and time.time()-row[1]<=TTL:
                cached=json.loads(row[0])
                try:
                    if cached.get("model")!=model or cached.get("engine_sha256")!=engine_sha256 or cached.get("status")!="ok" or set(cached.get("answers",{}))!=set(qs): raise ValueError
                    for i in qs: _answer(qs[i],cached["answers"][i])
                    source_usage=dict(cached.get("usage",{})); cached.update(source_usage=source_usage,usage={"input_tokens":0,"output_tokens":0},cache_hit=True,evaluations=0,latency_ms=0); con.execute("UPDATE backend_decision_metrics SET hits=hits+1"); con.commit(); return cached
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    con.execute("DELETE FROM backend_decision_cache WHERE k=?",(key,)); con.commit()
        con.execute("UPDATE backend_decision_metrics SET evaluations=evaluations+1"); con.commit(); out["evaluations"]=1; started=time.monotonic(); raw=request(state,qs)
        if not isinstance(raw,dict) or raw.get("model")!=model or set(raw.get("answers",{}))!=set(qs): raise ValueError("response")
        for i,q in qs.items(): _answer(q,raw["answers"][i])
        usage=raw.get("usage");
        if not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k]<0 for k in ("input_tokens","output_tokens")): raise ValueError("usage")
        out.update(status="ok",answers=raw["answers"],usage=usage,latency_ms=round((time.monotonic()-started)*1000,2)); con.execute("UPDATE backend_decision_metrics SET input_tokens=input_tokens+?,output_tokens=output_tokens+?",(usage["input_tokens"],usage["output_tokens"]))
        if key:
            con.execute("INSERT OR REPLACE INTO backend_decision_cache VALUES (?,?,?)",(key,json.dumps(out),time.time()))
            con.execute("DELETE FROM backend_decision_cache WHERE k IN (SELECT k FROM backend_decision_cache ORDER BY created DESC LIMIT -1 OFFSET ?)",(CAP,))
        con.commit(); return out
    except Exception:
        if con:
            try: con.execute("UPDATE backend_decision_metrics SET errors=errors+1"); con.commit()
            except sqlite3.Error: pass
        out["reason_codes"]=["invalid_request_or_response"]; return out
    finally:
        if con: con.close()
