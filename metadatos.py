"""Lee únicamente datos técnicos declarados para una pista del vídeo solicitado."""
import json
import math
import re
import sqlite3
import subprocess
import shutil
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from urllib.request import Request, urlopen

pool = ThreadPoolExecutor(max_workers=3)
KEYS = ("resolution", "bitrate", "clarity", "fps", "sound", "sync")

def empty():
    return dict.fromkeys(KEYS, "unknown")

def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0

def count(raw):
    if type(raw) is int and raw >= 0:
        return raw
    if isinstance(raw,str) and re.fullmatch(r"[0-9]{1,18}",raw):
        return int(raw)
    return None

def page_likes(page):
    # Restrict the search to the action bar of the main video, excluding comments.
    match=re.search(r"\bytInitialData\s*=\s*",page)
    if not match:
        return None
    try:
        data,_=json.JSONDecoder().raw_decode(page[match.end():].lstrip())
        content=data["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"]
    except (ValueError,KeyError,TypeError):
        return None
    def labels(node):
        if isinstance(node,list):
            for child in node: yield from labels(child)
        elif isinstance(node,dict):
            for key,value in node.items():
                if key in ("accessibilityText","accessibilityLabel","label") and isinstance(value,str):
                    yield value
                elif isinstance(value,(dict,list)):
                    yield from labels(value)
    for item in content:
        for label in labels(item.get("videoPrimaryInfoRenderer",{}).get("videoActions",{})):
            found=re.fullmatch(r"(?:like this video along with |like this video with )?([0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?: other people| likes?)",label.strip(),re.I)
            if found:
                return count(found[1].replace(",",""))
    return None

def extract(page, video_id):
    for match in re.finditer(r'\bytInitialPlayerResponse\s*=\s*', page):
        try:
            player, _ = json.JSONDecoder().raw_decode(page[match.end():].lstrip())
        except ValueError:
            continue
        if player.get("videoDetails", {}).get("videoId") != video_id:
            continue
        raw=player["videoDetails"].get("lengthSeconds","")
        duration=int(raw) if str(raw).isascii() and str(raw).isdigit() and int(raw)>0 else None
        result={"values":empty()}
        streams = player.get("streamingData", {})
        tracks = []
        for group in ("adaptiveFormats", "formats"):
            for f in streams.get(group, []):
                if f.get("mimeType","").startswith("video/"):
                    tracks.append((f,group=="adaptiveFormats"))
        candidates = [(f,video_only) for f,video_only in tracks if positive(f.get("height"))]
        if candidates:
            f,video_only = max(candidates,key=lambda pair:(pair[0]["height"],pair[0].get("fps") or 0,pair[1]))
            result=report(f, f.get("bitrate") if video_only else None, f.get("itag"), "YouTube")
        result.update(views=count(player["videoDetails"].get("viewCount")),likes=page_likes(page))
        result["duration"]=duration
        return result
    return {"values":empty()}

def report(track, bitrate, identifier, source):
    values=empty()
    values["resolution"]=f'{track["height"]}p'
    if positive(track.get("width")):
        values["resolution"]=f'{track["width"]} × {track["height"]} px'
    if positive(bitrate):
        values["bitrate"]=f'{round(bitrate)} bit/s'
    if positive(track.get("fps")):
        values["fps"]=f'{track["fps"]:g} fps'
    return dict(values=values,track=identifier,source=source,at=datetime.now(timezone.utc).isoformat())

def from_formats(data, vid):
    if data.get("id")!=vid:
        return {"values":empty()}
    tracks=[f for f in data.get("formats",[]) if positive(f.get("height")) and
            f.get("vcodec") not in (None,"none","images") and not f.get("has_drm")]
    duration=round(data["duration"]) if positive(data.get("duration")) else None
    stats=dict(duration=duration,views=count(data.get("view_count")),likes=count(data.get("like_count")))
    if not tracks:
        return {"values":empty(),**stats}
    f=max(tracks,key=lambda x:(x["height"],x.get("fps") or 0,x.get("acodec")=="none",x.get("vbr") or 0))
    bitrate=f.get("vbr")
    if not positive(bitrate) and f.get("acodec")=="none":
        bitrate=f.get("tbr")
    return dict(report(f,bitrate*1000 if positive(bitrate) else None,f.get("format_id"),"yt-dlp"),**stats)

lock=Lock()
active=set()

def busy(path, vid):
    with lock:
        return (path,vid) in active

def schedule(path, vid):
    key=(path,vid)
    with lock:
        if key in active:
            return
        active.add(key)
    def job():
        try:
            fetch(path,vid)
        finally:
            with lock:
                active.discard(key)
    pool.submit(job)

def extractor():
    local=Path(__file__).parent/".vqi-env"/"bin"/"yt-dlp"
    return str(local) if local.is_file() else shutil.which("yt-dlp")

def fetch(db_path, vid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}",vid):
        return
    link="https://www.youtube.com/watch?v="+vid
    try:
        req=Request(link,headers={"User-Agent":"Mozilla/5.0","Accept-Language":"en"})
        with urlopen(req,timeout=5) as response:
            info=extract(response.read(6_000_000).decode("utf-8","replace"),vid)
    except (OSError,ValueError):
        info={"values":empty()}
    if info.get("likes") is None or info.get("views") is None or not info.get("duration") or any(info["values"][k]=="unknown" for k in ("resolution","bitrate","fps")):
        tool=extractor()
        if tool:
            try:
                result=subprocess.run([tool,"--ignore-config","--no-playlist","--skip-download","--dump-single-json",
                                       "--no-warnings","--socket-timeout","8","--retries","0",link],
                                      capture_output=True,text=True,timeout=45,check=True)
                other=from_formats(json.loads(result.stdout),vid)
                observed={k:info.get(k) if info.get(k) is not None else other.get(k) for k in ("duration","likes","views")}
                if sum(v!="unknown" for v in other["values"].values())>=sum(v!="unknown" for v in info["values"].values()) and "at" in other:
                    info=other
                info.update(observed)
            except (OSError,subprocess.SubprocessError,ValueError):
                pass
    if any(info["values"][k]=="unknown" for k in ("resolution","bitrate","fps")):
        info["message"]=("YouTube no ha facilitado todos los datos de una pista verificable." if tool else
                         "Si faltan datos, ejecuta Activar_lector_VQI.command en el Mac del servidor y repite la consulta.")
    info.update(url=link,checked=datetime.now(timezone.utc).isoformat())
    with closing(sqlite3.connect(db_path,timeout=15)) as db, db:
        old=db.execute("SELECT info FROM technical WHERE id=?",(vid,)).fetchone()
        old_info=json.loads(old[0]) if old else {}
        stats={}
        for k in ("views","likes"):
            if info.get(k) is not None:
                stats.update({k:info[k],k+"At":info["checked"]})
            else:
                stats.update({k:old_info.get(k),k+"At":old_info.get(k+"At")})
        duration=info.get("duration") or (json.loads(old[0]).get("duration") if old else None)
        if old and "at" in json.loads(old[0]) and "at" not in info:
            previous=json.loads(old[0]);previous.update(checked=info["checked"],message="Se conserva la última lectura verificada.");info=previous
        info.update(stats,duration=duration)
        db.execute("INSERT OR REPLACE INTO technical VALUES(?,?)",(vid,json.dumps(info)))
