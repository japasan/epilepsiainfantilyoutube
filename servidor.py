"""Aplicación del estudio. Python 3.9+ con biblioteca estándar."""
import metadatos
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("ESTUDIO_DB", ROOT / "estudio.sqlite3"))


class Error(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def require(ok, status, message):
    if not ok:
        raise Error(status, message)


def password(value, salt):
    return hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 600000)


def account(db, code, login, secret, role):
    require(isinstance(login, str) and 0 < len(login.strip()) <= 100 and
            isinstance(secret, str) and 0 < len(secret) <= 256, 400, "Revisa el usuario y la clave.")
    salt = secrets.token_bytes(16)
    db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)",
               (code, login.strip().casefold(), salt, password(secret, salt), role))


def init():
    with closing(sqlite3.connect(DB)) as db, db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS accounts(code TEXT PRIMARY KEY, login TEXT UNIQUE, salt BLOB, hash BLOB, role TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, code TEXT, expires REAL);
        CREATE TABLE IF NOT EXISTS notebook(id INTEGER PRIMARY KEY, version INTEGER, body TEXT);
        CREATE TABLE IF NOT EXISTS technical(id TEXT PRIMARY KEY, info TEXT);
        CREATE TABLE IF NOT EXISTS failures(ip TEXT, at REAL);
        """)
        db.execute("BEGIN IMMEDIATE")
        if "simulated" not in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}:
            db.execute("ALTER TABLE sessions ADD COLUMN simulated TEXT")
        if not db.execute("SELECT 1 FROM notebook").fetchone():
            account(db, "pepe", "Pepe", os.environ["CLAVE_PEPE"], "Administrador")
            state = dict(study=str(uuid.uuid4()), videos=[], people=[dict(code="pepe", name="Pepe")], reviews={}, consensus={})
            db.execute("INSERT INTO notebook VALUES(1,0,?)", (json.dumps(state),))

        db.execute("CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)")
        if not db.execute("SELECT 1 FROM migrations WHERE name='default_researchers'").fetchone():
            state=json.loads(db.execute("SELECT body FROM notebook").fetchone()[0])
            for name in ("Irma","Laura"):
                login=name.casefold()
                if db.execute("SELECT 1 FROM accounts WHERE login=?",(login,)).fetchone():
                    continue
                person=next((p for p in state["people"] if p["name"].casefold()==login),None)
                if person and db.execute("SELECT 1 FROM accounts WHERE code=?",(person["code"],)).fetchone():
                    continue
                if not person:
                    person=dict(code=str(uuid.uuid4()),name=name)
                    state["people"].append(person)
                account(db,person["code"],login,os.environ["CLAVE_"+login.upper()],"Investigador")
            db.execute("UPDATE notebook SET version=version+1,body=? WHERE id=1",(json.dumps(state),))
            db.execute("INSERT INTO migrations VALUES('default_researchers')")


def complete(a):
    for i in range(1, 17):
        k = f"d{i}"
        if i == 2 and a.get("d1") == 1 and a.get(k) in (None, "NA"):
            continue
        if 3 <= i <= 15 and a.get(k) == "NA" and str(a.get(k+"_reason") or "").strip():
            continue
        if type(a.get(k)) is not int or not 1 <= a[k] <= 5:
            return False
    return type(a.get("gqs")) is int and 1 <= a["gqs"] <= 5 and all(
        type(a.get(f"j{i}")) is int and a[f"j{i}"] in (0, 1) for i in range(1, 5))


def migrate_assignments(state):
    by_video=state.setdefault("videoAssignments",{})
    for person,task in state.pop("assignments",{}).items():
        for vid in task.get("ordinary",[]):
            by_video.setdefault(vid,{"ordinary":[],"arbiter":None})["ordinary"].append(person)
        vid=task.get("arbiter")
        if vid:
            by_video.setdefault(vid,{"ordinary":[],"arbiter":None})["arbiter"]=person


def ordinary_reviews(reviews,assigned=()):
    return [(person,r) for person,r in reviews.items()
            if (not assigned or person in assigned) and r.get("role")!="dirimente"
            and isinstance(r.get("answers"),dict) and complete(r["answers"])]


def agreement(reviews,assigned=(),clinical=False):
    ordinary=sorted(ordinary_reviews(reviews,assigned),key=lambda item:(item[1].get("at") or "",item[0]))
    if len(ordinary)<2:
        return None
    left,right=(record["answers"] for _,record in ordinary[:2])
    pairs=[(left.get(f"d{i}"),right.get(f"d{i}")) for i in range(1,17)]
    reasons=[]
    item_reasons={}
    global_reasons=[]
    numeric=[[x for x in column if type(x) is int] for column in zip(*pairs)]
    a,b=numeric
    if 2*abs(sum(a)*len(b)-sum(b)*len(a))>len(a)*len(b):
        reasons.append("DISCERN: diferencia entre las medias superior a 0,5 puntos.")
        global_reasons.append(reasons[-1])
    for index,(x,y) in enumerate(pairs,1):
        if (type(x) is int)!=(type(y) is int):
            reasons.append(f"DISCERN, ítem {index}: discrepancia sobre su aplicabilidad.")
            item_reasons[f"d{index}"]=reasons[-1]
        elif type(x) is int and abs(x-y)>=2:
            reasons.append(f"DISCERN, ítem {index}: puntuaciones {min(x,y)} y {max(x,y)} (diferencia de {abs(x-y)} puntos).")
            item_reasons[f"d{index}"]=reasons[-1]
    discern=not reasons
    gqs=left["gqs"]==right["gqs"]
    if not gqs:
        reasons.append(f"GQS: puntuaciones distintas ({min(left['gqs'],right['gqs'])} y {max(left['gqs'],right['gqs'])}).")
        item_reasons["gqs"]=reasons[-1]
    jama=True
    for index,name in enumerate(("autoría","atribución","divulgación","actualización"),1):
        if left[f"j{index}"]!=right[f"j{index}"]:
            jama=False
            reasons.append(f"JAMA, {name}: un evaluador marca «Cumple» y el otro «No cumple».")
            item_reasons[f"j{index}"]=reasons[-1]
    clinical=bool(clinical or left.get("clinicalConcern") or right.get("clinicalConcern"))
    if clinical:
        reasons.append("Discrepancia clínica o de seguridad registrada.")
        global_reasons.append(reasons[-1])
    return dict(discern=discern and not clinical,gqs=gqs,jama=jama,clinical=clinical,
                agreed=not reasons,reasons=reasons,itemReasons=item_reasons,globalReasons=global_reasons)


def can_arbitrate(reviews,code,assigned=(),clinical=False):
    result=agreement({p:r for p,r in reviews.items() if p!=code},assigned,clinical)
    return bool(result and not result["agreed"])


def api(db, route, data, token, ip):
    now = time.time()
    if route == "login":
        db.execute("DELETE FROM failures WHERE at < ?", (now-300,))
        require(db.execute("SELECT count(*) FROM failures WHERE ip=?", (ip,)).fetchone()[0] < 20, 429, "Espera cinco minutos para volver a intentarlo.")
        user = db.execute("SELECT code,salt,hash FROM accounts WHERE login=?", (str(data.get("login","")).strip().casefold(),)).fetchone()
        secret = str(data.get("password",""))
        if len(secret)>256 or not hmac.compare_digest(password(secret, user[1] if user else bytes(16)), user[2] if user else bytes(32)):
            db.execute("INSERT INTO failures VALUES(?,?)", (ip,now))
            db.commit()
            raise Error(401, "Usuario o clave incorrectos.")
        new_token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires < ? OR token=?", (now,token))
        db.execute("INSERT INTO sessions(token,code,expires) VALUES(?,?,?)", (new_token,user[0],now+43200))
        return {"ok":True}, new_token
    user = db.execute("SELECT a.code,a.role,a.login,s.simulated FROM sessions s JOIN accounts a ON s.code=a.code WHERE s.token=? AND s.expires>?", (token,now)).fetchone()
    require(user, 401, "Inicia sesión para acceder al estudio.")
    code,role,login,simulated = user
    if route == "logout":
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return {"ok":True}, ""
    version,body = db.execute("SELECT version,body FROM notebook WHERE id=1").fetchone()
    state = json.loads(body)
    migrate_assignments(state)
    tasks=state["videoAssignments"]
    people = {p["code"]:p for p in state["people"]}
    require(code in people, 403, "La cuenta no está activa en este estudio.")
    admin = role == "Administrador"
    owner=code
    profiles=[]
    if admin:
        roles=dict(db.execute("SELECT code,role FROM accounts"))
        profiles=[p for p in people.values() if p["code"]==owner or roles.get(p["code"])=="Investigador"]
        if simulated in people and roles.get(simulated)=="Investigador":
            code,role,admin=simulated,"Investigador",False
        if route=="switch":
            require(data.get("actor")==code,409,"El perfil activo ha cambiado. Recarga el catálogo.")
            target=data.get("target")
            require(any(p["code"]==target for p in profiles),400,"Perfil desconocido.")
            db.execute("UPDATE sessions SET simulated=? WHERE token=?",(None if target==owner else target,token))
            return {"ok":True},None
    elif route=="switch":
        raise Error(403,"Solo el administrador puede cambiar de perfil.")
    assigned_ids={vid for vid,task in tasks.items() if code in task["ordinary"] or task.get("arbiter")==code}
    visible_videos=state["videos"] if admin else [v for v in state["videos"] if v["id"] in assigned_ids]
    clinical_ids={v["id"] for v in state["videos"] if v.get("clinicalReview")}
    def review_count(vid):
        return len(ordinary_reviews(state["reviews"].get(vid,{}),tasks.get(vid,{}).get("ordinary",[])))
    if route == "state":
        counts={v["id"]:review_count(v["id"]) for v in visible_videos}
        cached={vid:json.loads(raw) for vid,raw in db.execute("SELECT id,info FROM technical")}
        for video in visible_videos:
            info=cached.get(video["id"],{})
            if not video.get("duration"):
                if info.get("duration"):
                    video["duration"]=info["duration"]
            for k in ("views","likes"):
                if not video.get(k+"Manual") and info.get(k) is not None:
                    video[k]=info[k]
                    video[k+"At"]=info.get(k+"At")
            if admin and ("likes" not in info or "duration" not in info):
                metadatos.schedule(DB,video["id"])
        if admin:
            state.setdefault("technical",{}).update({v["id"]:cached[v["id"]] for v in visible_videos if v["id"] in cached})
        ready={vid:can_arbitrate(state["reviews"].get(vid,{}),code,task["ordinary"],vid in clinical_ids)
               for vid,task in tasks.items() if task.get("arbiter")==code}
        if not admin:
            state = dict(study=state["study"],videos=visible_videos,people=[people[code]],consensus={},videoAssignments={vid:dict(ordinary=[code] if code in task["ordinary"] else [],arbiter=code if task.get("arbiter")==code else None) for vid,task in tasks.items() if code in task["ordinary"] or task.get("arbiter")==code},
                         reviews={v:{code:r[code]} for v,r in state["reviews"].items() if v in assigned_ids and code in r})
        accounts = dict(db.execute("SELECT code,login FROM accounts")) if admin else {}
        policies={vid:agreement(records,tasks.get(vid,{}).get("ordinary",[]),vid in clinical_ids) for vid,records in state["reviews"].items()} if admin else {}
        return dict(policies=policies,profiles=profiles,owner=owner,arbiterReady=ready,state=state,counts=counts,version=version,user=dict(code=code,name=people[code]["name"],role=role,login=login),accounts=accounts), None
    require(data.get("actor")==code,409,"La sesión ha cambiado. Vuelve a entrar.")
    if route=="results":
        names={p["code"]:p["name"] for p in state.get("archivedPeople",[])+state["people"]}
        videos=[]
        for v in state["videos"]:
            reviews=state["reviews"].get(v["id"],{})
            validated=v.get("validated") is True
            assigned=tasks.get(v["id"],{}).get("ordinary",[])
            policy=agreement(reviews,assigned,v["id"] in clinical_ids)
            personal=not admin and policy is None
            if personal or (not validated and policy is None):
                eligible=tasks.get(v["id"],{})
                reviewers=set(eligible.get("ordinary",[]))|{eligible.get("arbiter")}
                if personal:
                    reviewers &= {code}
                if not any(p in reviewers and complete(r.get("answers") or {}) for p,r in reviews.items()):
                    continue
            ordered=sorted(reviews,key=lambda c:(not complete(reviews[c].get("answers") or {}),reviews[c].get("at",""),c))
            codes=assigned or [c for c in ordered if reviews[c].get("role")!="dirimente"]
            if personal:
                codes=[code]
            rows=[]
            for person in codes[:2]:
                record=reviews.get(person,{})
                answers=record.get("answers",{})
                # The summary uses the presence of an NA reason, never its text.
                values={k:answers[k] for k in [*(f"d{i}" for i in range(1,17)),"gqs",*(f"j{i}" for i in range(1,5))] if k in answers}
                for i in range(3,16):
                    key=f"d{i}_reason"
                    if str(answers.get(key) or "").strip():
                        values[key]="NA"
                rows.append(dict(name=names.get(person,person),answers=values,status=record.get("status")))
            scores=state.get("vqi",{}).get(v["id"],{}).get("scores",{})
            components=[scores.get(key) for key in metadatos.KEYS]
            total=sum(components) if all(type(x) is int for x in components) else None
            videos.append(dict(id=v["id"],title=v.get("title") or v["id"],rows=rows,validated=validated,personal=personal,vqi=None if personal else total,agreement=None if personal else policy,clinicalReview=v["id"] in clinical_ids,arbitrated=any(r.get("role")=="dirimente" and r.get("status")=="final" for r in reviews.values())))
        return {"videos":videos},None
    if route == "password":
        current,new=data.get("current"),data.get("new")
        require(isinstance(current,str) and len(current)<=256 and isinstance(new,str) and 0<len(new)<=256,400,"Revisa la clave introducida.")
        salt,old=db.execute("SELECT salt,hash FROM accounts WHERE code=?",(code,)).fetchone()
        require(hmac.compare_digest(password(current,salt),old),403,"La clave actual es incorrecta.")
        salt=secrets.token_bytes(16)
        db.execute("UPDATE accounts SET salt=?,hash=? WHERE code=?",(salt,password(new,salt),code))
        db.execute("DELETE FROM sessions WHERE code=? AND token<>?",(code,token))
        return {"ok":True},None
    if route=="catalog-metrics":
        refresh=data.get("refresh") is True
        if refresh:
            require(admin,403,"Solo el administrador puede actualizar los datos de YouTube.")
        cached={vid:json.loads(raw) for vid,raw in db.execute("SELECT id,info FROM technical")}
        rows=[]
        for video in visible_videos:
            vid=video["id"]
            if refresh:
                metadatos.schedule(DB,vid)
            info=cached.get(vid,{})
            row={"id":vid,"finalCount":review_count(vid),"duration":video.get("duration") or info.get("duration")}
            for key in ("views","likes","viewsAt","likesAt"):
                manual=video.get(key.removesuffix("At")+"Manual")
                row[key]=info.get(key) if not manual and info.get(key) is not None else video.get(key)
            rows.append(row)
        return {"rows":rows,"pending":any(metadatos.busy(DB,v["id"]) for v in visible_videos)},None
    if route in ("metadata","metadata-status"):
        require(admin,403,"Solo el administrador puede consultar las pistas.")
        vid=data.get("id")
        require(any(v["id"]==vid for v in state["videos"]),404,"Vídeo desconocido.")
        if route=="metadata":
            metadatos.schedule(DB,vid)
        row=db.execute("SELECT info FROM technical WHERE id=?",(vid,)).fetchone()
        info=json.loads(row[0]) if row else state.get("technical",{}).get(vid)
        return {"pending":metadatos.busy(DB,vid),"info":info},None
    if route == "review":
        vid = data.get("id")
        require(any(v["id"]==vid for v in state["videos"]),404,"El vídeo ya no está en el catálogo.")
        require(admin or vid in assigned_ids,403,"Este vídeo no está asignado a tu perfil.")
        reviews=state["reviews"].setdefault(vid,{})
        old=reviews.get(code)
        require(data.get("previous")==old,409,"Esta valoración ha cambiado. Recarga para ver los datos guardados.")
        require(not old or old.get("status")!="final",409,"Esta valoración ya está finalizada.")
        answers,status=data.get("answers"),data.get("status")
        require(isinstance(answers,dict) and status in ("draft","final") and (status=="draft" or complete(answers)),400,"Revisa las respuestas antes de finalizar.")
        record=dict(answers=answers,status=status,at=datetime.now(timezone.utc).isoformat())
        assigned=tasks.get(vid,{})
        record["role"]="dirimente" if assigned.get("arbiter")==code else "ordinario"
        if record["role"]=="dirimente":
            require(can_arbitrate(reviews,code,assigned.get("ordinary",[]),vid in clinical_ids),409,"El dirimente interviene cuando hay al menos dos valoraciones completas con discrepancias.")
            for _,source in sorted(ordinary_reviews(reviews,assigned.get("ordinary",[])),key=lambda item:(item[1].get("at") or "",item[0]))[:2]:
                source["status"]="final"
        reviews[code]=record
        result={"record":record}
    elif route in ("admin","account","edit","delete","vqi","assignment","metric","validation"):
        require(admin,403,"Solo el administrador puede realizar esta operación.")
        require(version==data.get("version"),409,"El estudio ha cambiado. Recarga la página antes de continuar.")
        if route=="validation":
            video=next((v for v in state["videos"] if v["id"]==data.get("id")),None)
            require(video is not None,404,"Vídeo desconocido.")
            require(type(data.get("validated")) is bool,400,"Estado de validación incorrecto.")
            video["validated"]=data["validated"]
        elif route=="assignment":
            vid=data.get("id")
            require(any(v["id"]==vid for v in state["videos"]),404,"Vídeo desconocido.")
            ordinary,arbiter=data.get("ordinary"),data.get("arbiter")
            require(isinstance(ordinary,list) and len(ordinary)<=2,400,"Selecciona hasta dos evaluadores ordinarios.")
            prior=tasks.get(vid,{})
            allowed=set(people)|set(prior.get("ordinary",[]))
            chosen=ordinary+([arbiter] if arbiter is not None else [])
            require(all(isinstance(c,str) and c in allowed for c in chosen) and len(set(chosen))==len(chosen),400,"Elige personas distintas para cada función.")
            require(arbiter is None or arbiter in people,400,"Selecciona un dirimente activo.")
            reviews=state["reviews"].get(vid,{})
            if prior.get("arbiter")!=arbiter:
                require(not reviews.get(prior.get("arbiter")),409,"El dirimente anterior ya ha iniciado su valoración.")
                require(not reviews.get(arbiter),409,"Esta persona ya ha iniciado una valoración del vídeo.")
            if reviews.get(arbiter):
                require(set(ordinary)==set(prior.get("ordinary",[])),409,"El dirimente ya ha iniciado su valoración con la pareja anterior.")
            tasks[vid]=dict(ordinary=ordinary,arbiter=arbiter)
        elif route=="metric":
            video=next((v for v in state["videos"] if v["id"]==data.get("id")),None)
            key=data.get("key");value=data.get("value")
            require(video is not None and key in ("views","likes"),400,"Campo no válido.")
            require(value is None or type(value) is int and 0<=value<=9007199254740991,400,"Introduce un entero igual o mayor que cero.")
            video[key]=value
            video[key+"Manual"]=value is not None
            video[key+"At"]=datetime.now(timezone.utc).isoformat() if value is not None else None
        elif route in ("edit","delete"):
            pid=data.get("code")
            require(pid in people and pid!=code,400,"Selecciona una cuenta de investigador.")
            existing=db.execute("SELECT role FROM accounts WHERE code=?",(pid,)).fetchone()
            require(not existing or existing[0]=="Investigador",403,"Cuenta de administrador protegida.")
            if route=="delete":
                for vid,task in tasks.items():
                    task["ordinary"]=[c for c in task["ordinary"] if c!=pid or state["reviews"].get(vid,{}).get(pid,{}).get("status")=="final"]
                    if task.get("arbiter")==pid:
                        task["arbiter"]=None
                db.execute("DELETE FROM accounts WHERE code=?",(pid,))
                state["people"]=[p for p in state["people"] if p["code"]!=pid]
                state.setdefault("archivedPeople",[]).append(people[pid])
            else:
                name=str(data.get("name","")).strip()
                login=str(data.get("login","")).strip().casefold()
                secret=data.get("password","")
                require(0<len(name)<=120 and 0<len(login)<=100 and len(secret)<=256,400,"Revisa nombre, usuario y clave.")
                require(all(p["code"]==pid or p["name"].casefold()!=name.casefold() for p in people.values()),409,"Ese nombre ya está registrado.")
                people[pid]["name"]=name
                if not existing:
                    account(db,pid,login,secret,"Investigador")
                elif secret:
                    salt=secrets.token_bytes(16)
                    db.execute("UPDATE accounts SET login=?,salt=?,hash=? WHERE code=?",(login,salt,password(secret,salt),pid))
                else:
                    db.execute("UPDATE accounts SET login=? WHERE code=?",(login,pid))
            db.execute("DELETE FROM sessions WHERE code=?",(pid,))
        elif route=="vqi":
            vid=data.get("id")
            require(any(v["id"]==vid for v in state["videos"]),404,"Vídeo desconocido.")
            scores=data.get("scores")
            require(isinstance(scores,dict) and set(scores)==set(metadatos.KEYS),400,"Faltan componentes del VQI.")
            for key,maximum in zip(metadatos.KEYS,[20,20,20,15,15,10]):
                val=scores[key]
                require(val=="unknown" or type(val) is int and 0<=val<=maximum,400,"Puntuación VQI fuera de rango.")
            state.setdefault("vqi",{})[vid]=dict(scores=scores,by=code,at=datetime.now(timezone.utc).isoformat())
        elif route=="account":
            name=str(data.get("name","")).strip()
            require(0<len(name)<=120,400,"Introduce el nombre.")
            person=next((p for p in people.values() if p["name"].casefold()==name.casefold()),None)
            pid=person["code"] if person else str(uuid.uuid4())
            account(db,pid,data.get("login"),data.get("password"),"Investigador")
            if not person:
                state["people"].append(dict(name=name,code=pid))
        else:
            old_ids={v["id"] for v in state["videos"]}
            state=data["state"]
            require(isinstance(state.get("videos"),list) and isinstance(state.get("reviews"),dict) and isinstance(state.get("people"),list),400,"Datos del estudio no válidos.")
            if code not in {p["code"] for p in state["people"]}:
                state["people"].append(people[code])
            for video in state["videos"]:
                if video["id"] not in old_ids:
                    metadatos.schedule(DB,video["id"])
        result={}
    else:
        raise Error(404,"Operación desconocida.")
    db.execute("UPDATE notebook SET version=version+1,body=? WHERE id=1",(json.dumps(state,ensure_ascii=False),))
    return dict(result,version=version+1),None


class Handler(BaseHTTPRequestHandler):
    def send(self,status,body,mime,cookie=None):
        self.send_response(status)
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        if cookie is not None:
            attr="; Secure" if os.environ.get("HTTPS_COOKIE")=="1" else ""
            self.send_header("Set-Cookie","epi_session="+cookie+"; Path=/; HttpOnly; SameSite=Strict"+attr+("; Max-Age=0" if not cookie else ""))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        manuals={"DISCERN":"DISCERN.html","GQS":"GQS.pdf","JAMA":"JAMA.pdf","VQI":"VQI.html"}
        if self.path.startswith("/manual/"):
            name=self.path[8:]
            if name not in manuals:
                self.send_error(404);return
            jar=cookies.SimpleCookie(self.headers.get("Cookie",""))
            token=jar["epi_session"].value if "epi_session" in jar else ""
            with closing(sqlite3.connect(DB)) as db:
                row=db.execute("SELECT role,simulated FROM accounts JOIN sessions USING(code) WHERE token=? AND expires>?",(token,time.time())).fetchone()
            if not row or (name=="VQI" and (row[0]!="Administrador" or row[1])):
                self.send_error(403);return
            file=ROOT/"manuales"/manuals[name]
            self.send(200,file.read_bytes(),"application/pdf" if file.suffix==".pdf" else "text/html; charset=utf-8");return
        if self.path!="/":
            self.send_error(404)
            return
        self.send(200,(ROOT/"Epilepsia_en_YouTube.html").read_bytes(),"text/html; charset=utf-8")

    def do_POST(self):
        cookie=None
        try:
            require(self.path.startswith("/api/") and self.headers.get("X-Estudio")=="1",403,"Solicitud no permitida.")
            size=int(self.headers.get("Content-Length",0))
            require(0<size<=22_000_000,413,"Tamaño de solicitud no permitido.")
            data=json.loads(self.rfile.read(size))
            require(isinstance(data,dict),400,"Solicitud no válida.")
            jar=cookies.SimpleCookie(self.headers.get("Cookie",""))
            token=jar["epi_session"].value if "epi_session" in jar else ""
            with closing(sqlite3.connect(DB,timeout=15)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                result,cookie=api(db,self.path[5:],data,token,self.client_address[0])
            status=200
        except Error as err:
            status,result=err.status,{"error":str(err)}
        except sqlite3.IntegrityError:
            status,result=409,{"error":"Ya existe una cuenta con ese usuario o investigador."}
        except (ValueError,KeyError,TypeError):
            status,result=400,{"error":"Datos no válidos."}
        except sqlite3.Error:
            status,result=503,{"error":"No se pudo guardar en la base de datos. Vuelve a intentarlo."}
        self.send(status,json.dumps(result).encode(),"application/json",cookie)


if __name__=="__main__":
    import webbrowser
    init()
    host=os.environ.get("HOST","127.0.0.1")
    port=int(os.environ.get("PORT","8765"))
    server=ThreadingHTTPServer((host,port),Handler)
    print(f"Estudio disponible en http://{host}:{port}",flush=True)
    if host=="127.0.0.1":
        webbrowser.open(f"http://{host}:{port}")
    server.serve_forever()
