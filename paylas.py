"""Halis Dede otomatik paylaşım.
takvim.json'da zamanı gelmiş ve durum.json'da olmayan gönderileri Instagram'a paylaşır:
Reels (video + kapak + açıklama) -> sabit yorum metni yorum olarak -> aynı video hikâye olarak.
Anahtar yalnızca IG_TOKEN ortam değişkeninden okunur ve hiçbir yere yazdırılmaz (kayıtlar herkese açık).
Kullanım: python paylas.py            (zamanı gelenleri paylaş)
          python paylas.py --deneme   (hiçbir şey paylaşmadan anahtarı ve hesabı kontrol et)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com/v22.0"
ROOT = Path(__file__).resolve().parent
TOKEN = os.environ.get("IG_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "productfactory0/halisdede-paylasim")
SHA = os.environ.get("GITHUB_SHA", "main")


def api(method, path, **params):
    params["access_token"] = TOKEN
    r = requests.request(method, f"{GRAPH}/{path}", params=params if method == "GET" else None,
                         data=None if method == "GET" else params, timeout=120)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", body)
        msg = err.get("message", str(err))[:300] if isinstance(err, dict) else str(err)[:300]
        raise RuntimeError(f"{method} {path.split('?')[0]} -> {r.status_code}: {msg}")
    return body


def ig_user_id():
    data = api("GET", "me/accounts", fields="name,instagram_business_account{id,username}")
    for page in data.get("data", []):
        iba = page.get("instagram_business_account")
        if iba:
            print(f"Sayfa: {page.get('name')} -> Instagram: @{iba.get('username')}")
            return iba["id"]
    raise RuntimeError("Anahtarla erişilen sayfada bağlı Instagram hesabı bulunamadı.")


def public_url(rel):
    # jsDelivr, açık depodaki dosyayı doğru içerik türüyle sunar; commit'e sabitlenir.
    return f"https://cdn.jsdelivr.net/gh/{REPO}@{SHA}/{rel}"


def upload_video(container_uri, video: Path):
    size = video.stat().st_size
    with video.open("rb") as f:
        r = requests.post(container_uri, data=f, timeout=600, headers={
            "Authorization": f"OAuth {TOKEN}", "offset": "0", "file_size": str(size)})
    if r.status_code >= 400:
        raise RuntimeError(f"video yükleme -> {r.status_code}: {r.text[:300]}")


def wait_ready(cid, label):
    for _ in range(60):  # en fazla ~10 dk
        st = api("GET", cid, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{label}: Instagram işleyemedi ({st})")
        time.sleep(10)
    raise RuntimeError(f"{label}: işleme zaman aşımı")


def video_container(ig, video, **fields):
    c = api("POST", f"{ig}/media", upload_type="resumable", **fields)
    upload_video(c["uri"], video)
    return c["id"]


def post(ig, item):
    d = ROOT / item["klasor"]
    video = d / "video.mp4"
    caption = (d / "aciklama.txt").read_text(encoding="utf-8").strip()
    fields = {"media_type": "REELS", "caption": caption, "share_to_feed": "true"}
    if (d / "kapak.jpg").exists():
        fields["cover_url"] = public_url(f"{item['klasor']}/kapak.jpg")
    cid = video_container(ig, video, **fields)
    wait_ready(cid, "reels")
    media = api("POST", f"{ig}/media_publish", creation_id=cid)["id"]
    print(f"[OK] {item['id']} reels paylaşıldı (media {media})")
    out = {"reels": media, "zaman": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    yorum = d / "sabit-yorum.txt"
    if yorum.exists():
        try:
            out["yorum"] = api("POST", f"{media}/comments", message=yorum.read_text(encoding="utf-8").strip())["id"]
            print(f"[OK] {item['id']} yorum yazıldı (sabitlemeyi telefondan yap)")
        except RuntimeError as e:
            print(f"[!] {item['id']} yorum yazılamadı: {e}")

    if item.get("hikaye", True):
        try:
            sid = video_container(ig, video, media_type="STORIES")
            wait_ready(sid, "hikâye")
            out["hikaye"] = api("POST", f"{ig}/media_publish", creation_id=sid)["id"]
            print(f"[OK] {item['id']} hikâye paylaşıldı")
        except RuntimeError as e:
            print(f"[!] {item['id']} hikâye paylaşılamadı: {e}")
    return out


def main():
    if not TOKEN:
        sys.exit("IG_TOKEN yok (GitHub: Settings -> Secrets and variables -> Actions).")
    ig = ig_user_id()
    if "--deneme" in sys.argv:
        print("Deneme tamam: anahtar çalışıyor, hesap bulundu. Hiçbir şey paylaşılmadı.")
        return
    takvim = json.loads((ROOT / "takvim.json").read_text(encoding="utf-8"))
    durum_f = ROOT / "durum.json"
    durum = json.loads(durum_f.read_text(encoding="utf-8")) if durum_f.exists() else {}
    now = datetime.now(timezone.utc)
    hata = False
    for item in takvim:
        if item["id"] in durum:
            continue
        when = datetime.fromisoformat(item["zaman"])
        if when > now:
            print(f"[..] {item['id']} bekliyor ({item['zaman']})")
            continue
        try:
            durum[item["id"]] = post(ig, item)
        except RuntimeError as e:
            hata = True
            print(f"[HATA] {item['id']}: {e}")
        durum_f.write_text(json.dumps(durum, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        break  # bir çalışmada en fazla bir gönderi
    if hata:
        raise RuntimeError("paylaşım hatası (yukarıdaki [HATA] satırı)")


if __name__ == "__main__":
    try:
        main()
        (ROOT / "son-hata.txt").write_text("yok" + chr(10), encoding="utf-8")
    except Exception as e:  # noqa: BLE001  (anahtar mesajlarda yer almaz, api() onu yazdırmaz)
        zaman = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (ROOT / "son-hata.txt").write_text(f"{zaman} {type(e).__name__}: {str(e)[:400]}" + chr(10), encoding="utf-8")
        raise
