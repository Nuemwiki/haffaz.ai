from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Query
from google import genai
from google.genai import types, errors
import os
import json
from pydantic import BaseModel
import re
from typing import List, Optional
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from collections import defaultdict

# --- VERİ SETİ ---
from kuran_data import SURE_BASLANGIC_SAYFASI, SURE_SAYFA_DURAKLARI

load_dotenv(override=True)

api_key = os.getenv("GOOGLE_API_KEY")
model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash") # En güncel model varsayılan yapıldı

print("====================================================")
print("SISTEM YUKLENDI")
print("Model:", model_name)
print("API Key:", api_key[:15] + "..." if api_key else "None")
print("====================================================")

if not api_key:
    raise ValueError("GOOGLE_API_KEY bulunamadı!")

# Initialize Google GenAI client
client = genai.Client(api_key=api_key)

class Mutesabih(BaseModel):
    sure_no: int
    ayet_no: int

class QuranAnalysis(BaseModel):
    sure_no: Optional[int] = None
    ayet_no: Optional[int] = None
    mutesabihler: List[Mutesabih] = []
    okunan_kelimeler: Optional[str] = None

# --- SİSTEM TALİMATI (Performans ve Token Tasarrufu Odaklı) ---
# Arapça metin ve Türkçe meal verilerini modelin üretmesini engelleyerek 
# çıktı token sayısını %95 oranında azaltıyoruz. Model sadece koordinatları döndürecek.
system_instruction = """
GÖREV: Sesteki Kur'an okuyuşunu veya lafızlarını tespit et ve koordinatlarını yaz.
KURALLAR:
1. Seste kısa bile olsa bir Kur'an lafzı/ayeti duyulduysa mutlaka en uygun ana ayeti ("sure_no" ve "ayet_no") bul. Sadece sesteki gürültü/konuşma Kur'an dışıysa {} dön.
2. okunan_kelimeler: Seste duyulan kelimelerin TAMAMINI harekesiz sade Arapça olarak yaz (Örn: "يا ايها الانسان").
3. Sadece geçerli JSON döndür, başka açıklama ekleme.
ŞABLON: {"sure_no":82,"ayet_no":6,"okunan_kelimeler":"يا ايها الانسان"}
"""

# --- LİMİT SİSTEMİ (JSON Veritabanı ile Kalıcı) ---
LIMITS_FILE = os.path.join(os.path.dirname(__file__), "user_limits.json")
GUNLUK_LIMIT_UCRETSIZ = 5

def load_limits():
    if os.path.exists(LIMITS_FILE):
        try:
            with open(LIMITS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    if v.get("tarih"):
                        v["tarih"] = datetime.strptime(v["tarih"], "%Y-%m-%d").date()
                default_limits = defaultdict(lambda: {"tarih": None, "kullanim": 0, "premium": False})
                default_limits.update(data)
                return default_limits
        except Exception as e:
            print("Limit okuma hatası:", e)
    return defaultdict(lambda: {"tarih": None, "kullanim": 0, "premium": False})

def save_limits(limits):
    try:
        serializable = {}
        for k, v in limits.items():
            serializable[k] = {
                "tarih": v["tarih"].strftime("%Y-%m-%d") if v["tarih"] else None,
                "kullanim": v["kullanim"],
                "premium": v["premium"]
            }
        with open(LIMITS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=4)
    except Exception as e:
        print("Limit kaydetme hatası:", e)

# Load limits at startup
kullanici_limitler = load_limits()

def limit_kontrol(kullanici_id: str, is_premium: bool = False):
    global kullanici_limitler
    bugun = datetime.now().date()
    kayit = kullanici_limitler[kullanici_id]
    
    if kayit["tarih"] != bugun:
        kayit["tarih"] = bugun
        kayit["kullanim"] = 0
        kayit["premium"] = is_premium
    
    if is_premium or kayit["premium"]:
        save_limits(kullanici_limitler)
        return True, None
        
    if kayit["kullanim"] >= GUNLUK_LIMIT_UCRETSIZ:
        save_limits(kullanici_limitler)
        return False, {"limit_doldu": True, "kalan": 0, "limit": GUNLUK_LIMIT_UCRETSIZ}
    
    kayit["kullanim"] += 1
    save_limits(kullanici_limitler)
    return True, {"limit_doldu": False, "kalan": GUNLUK_LIMIT_UCRETSIZ - kayit["kullanim"], "limit": GUNLUK_LIMIT_UCRETSIZ}

def limit_iade_et(kullanici_id: str):
    global kullanici_limitler
    kayit = kullanici_limitler[kullanici_id]
    if kayit["kullanim"] > 0:
        kayit["kullanim"] -= 1
        save_limits(kullanici_limitler)

# --- YEREL KURAN VERİTABANI YÜKLEME ---
QURAN_DB_FILE = os.path.join(os.path.dirname(__file__), "quran_db.json")
quran_db = {}
if os.path.exists(QURAN_DB_FILE):
    try:
        with open(QURAN_DB_FILE, "r", encoding="utf-8") as f:
            quran_db = json.load(f)
        print(f"Yerel Kur'an veritabanı yüklendi. Ayet sayısı: {len(quran_db)}")
    except Exception as e:
        print("Kur'an veritabanı yükleme hatası:", e)

SURE_ADLARI = {
    1: "Fatiha", 2: "Bakara", 3: "Al-i İmran", 4: "Nisa", 5: "Maide", 6: "En'am", 7: "A'raf", 8: "Enfal", 9: "Tevbe", 10: "Yunus",
    11: "Hud", 12: "Yusuf", 13: "Ra'd", 14: "İbrahim", 15: "Hicr", 16: "Nahl", 17: "İsra", 18: "Kehf", 19: "Meryem", 20: "Taha",
    21: "Enbiya", 22: "Hac", 23: "Mü'minun", 24: "Nur", 25: "Furkan", 26: "Şuara", 27: "Neml", 28: "Kasas", 29: "Ankebut", 30: "Rum",
    31: "Lokman", 32: "Secde", 33: "Ahzab", 34: "Sebe", 35: "Fatır", 36: "Yasin", 37: "Saffat", 38: "Sad", 39: "Zümer", 40: "Mü'min",
    41: "Fussilet", 42: "Şura", 43: "Zuhruf", 44: "Duhan", 45: "Casiye", 46: "Ahkaf", 47: "Muhammed", 48: "Fetih", 49: "Hucurat", 50: "Kaf",
    51: "Zariyat", 52: "Tur", 53: "Necm", 54: "Kamer", 55: "Rahman", 56: "Vakıa", 57: "Hadid", 58: "Mücadele", 59: "Haşr", 60: "Mümtehine",
    61: "Saff", 62: "Cuma", 63: "Münafikun", 64: "Tegabun", 65: "Talak", 66: "Tahrim", 67: "Mülk", 68: "Kalem", 69: "Hakka", 70: "Mearic",
    71: "Nuh", 72: "Cin", 73: "Müzzemmil", 74: "Müddessir", 75: "Kıyamet", 76: "İnsan", 77: "Mürselat", 78: "Nebe", 79: "Naziat", 80: "Abese",
    81: "Tekvir", 82: "İnfitar", 83: "Mutaffifin", 84: "İnşikak", 85: "Buruc", 86: "Tarık", 87: "A'la", 88: "Gaşiye", 89: "Fecr", 90: "Beled",
    91: "Şems", 92: "Leyl", 93: "Duha", 94: "İnşirah", 95: "Tin", 96: "Alak", 97: "Kadir", 98: "Beyyine", 99: "Zilzal", 100: "Adiyat",
    101: "Karia", 102: "Tekasür", 103: "Asr", 104: "Hümeze", 105: "Fil", 106: "Kureyş", 107: "Maun", 108: "Kevser", 109: "Kafirun", 110: "Nasr",
    111: "Tebbet", 112: "İhlas", 113: "Felak", 114: "Nas"
}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"durum": "Hafiz AI - Konum Modu Aktif", "model": model_name, "db_loaded": len(quran_db) > 0}

@app.get("/gunun-ayeti")
def gunun_ayeti():
    try:
        if os.path.exists("gunun_ayeti.json"):
            with open("gunun_ayeti.json", "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "text": "Şüphesiz Allah sabredenlerle beraberdir.",
        "ref": "Bakara Suresi, 153. Ayet"
    }

def temizle_harakat(text):
    return re.sub(r'[\u064B-\u065F\u0670]', '', text)

def clean_json(text):
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    
    if text.startswith("```json"): text = text[7:]
    elif text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
    return text.strip()

def norm_quran_word_list(text: str) -> List[str]:
    t = temizle_harakat(text)
    t = t.replace('ياايها', 'يا ايها').replace('يأيها', 'يا ايها')
    t = ''.join(c if (u'\u0621' <= c <= u'\u064A' or c == ' ') else '' for c in t)
    t = re.sub(r'\s+', ' ', t).strip()
    return [w for w in t.split() if len(w) >= 1]

def highlight_read_portion(full_text, read_portion):
    # Basit bir vurgulama mekanizması (Markdown bold)
    # Burada kelime eşleşmesi veya regex ile yapılabilir
    return full_text.replace(read_portion, f"**{read_portion}**")

def find_exact_mutashabihat_in_db(okunan_kelimeler: str, main_sure: int, main_ayet: int) -> List[Tuple[int, int]]:
    1. Birden fazla kelime okunduysa (örn: "يا ايها الانسان" -> 3 kelime):
       Bu kelimelerin HEPSİNİ müstakil kelime olarak içeren TÜM ayetler bulunur.
       Sadece 1 kelimesi ("انسان") geçiyor diye alakasız ayetleri KESİNLİKLE eklemez.
    2. Tek kelime okunduysa (örn: "القارعة"):
       O tek kelimenin geçtiği TÜM ayetleri müteşabih olarak ekler.
    """
    if not okunan_kelimeler or not quran_db:
        return []
        
    clean_words = norm_quran_word_list(okunan_kelimeler)
    if not clean_words:
        return []

    matched_pairs = []
    main_key = f"{main_sure}:{main_ayet}"

    for key, item in quran_db.items():
        if key == main_key:
            continue
            
        ar_text = item.get("ar", "")
        verse_words_set = set(norm_quran_word_list(ar_text))
        
        if len(clean_words) == 1:
            # TEK KELİME OKUNDUYSA: O kelimenin tam kelime olarak geçtiği tüm ayetleri bul
            target_word = clean_words[0]
            if target_word in verse_words_set:
                sure_no, ayet_no = map(int, key.split(":"))
                matched_pairs.append((sure_no, ayet_no))
        else:
            # ÇOKLU KELİME OKUNDUYSA: Okunan kelimelerin HEPSİNİN geçtiği ayetleri bul
            if all(target_word in verse_words_set for target_word in clean_words):
                sure_no, ayet_no = map(int, key.split(":"))
                matched_pairs.append((sure_no, ayet_no))
                
    return matched_pairs

@app.post("/analiz-et")
async def analiz_et(
    file: UploadFile = File(...), 
    x_user_id: str = Header(None, alias="X-User-ID"), 
    x_premium: str = Header("false", alias="X-Premium")
):
    limit_harcandi = False
    kullanici_id = x_user_id or "anonim"
    is_premium = x_premium.lower() == "true"
    try:
        izin_var, limit_bilgisi = limit_kontrol(kullanici_id, is_premium)
        if not izin_var: 
            raise HTTPException(status_code=429, detail=limit_bilgisi)
        
        limit_harcandi = True
        
        content = await file.read()
        mime_type = file.content_type or "audio/m4a"

        # Ask Gemini to find the verse and ALL similar verses (mutashabihat)
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(
                    data=content,
                    mime_type=mime_type
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=800,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                system_instruction=system_instruction
            )
        )
        
        print("--- YAPAY ZEKA YANITI ---")
        print(repr(response.text))
        if response.candidates:
            print("Finish Reason:", response.candidates[0].finish_reason)
            print("Safety Ratings:", response.candidates[0].safety_ratings)
        print("-------------------------")
        cleaned = clean_json(response.text)
        try:
            raw_data = json.loads(cleaned)
        except json.JSONDecodeError:
            print("--- HATA: Çözümlenemeyen Yanıt ---")
            print("CLEANED:", repr(cleaned))
            print("---------------------------------")
            raise HTTPException(
                status_code=500,
                detail={"hata": "Model geçersiz JSON yanıtı üretti", "raw": response.text, "limit_bilgisi": limit_bilgisi}
            )

        # Normalize to list (we only want exactly one primary match)
        sonuclar = []
        if isinstance(raw_data, dict):
            if raw_data.get("sure_no"):
                sonuclar = [raw_data]
        elif isinstance(raw_data, list):
            # Take only the first matched item to prevent listing unrelated guesses
            sonuclar = raw_data[:1] if raw_data else []

        # --- SAYFA VE KONUM HESAPLAMA & YEREL VERİLERLE ZENGİNLEŞTİRME ---
        final_sonuclar = []
        seen_keys = set()

        for item in sonuclar:
            sure_no = item.get("sure_no")
            ayet_no = item.get("ayet_no")
            
            if not sure_no or sure_no == 0: 
                continue

            # 1. Ana Ayeti Ekle
            key = f"{sure_no}:{ayet_no}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            db_item = quran_db.get(key, {"ar": "", "tr": "", "page": 1, "pos": "orta"})
            
            ar_text = db_item.get("ar", "")
            # Fatiha (Sure 1) hariç, 1. ayetlerin başındaki besmeleyi temizle
            if sure_no != 1 and ayet_no == 1:
                bismillah = "\u0628\u0650\u0633\u0652\u0645\u0650 \u0671\u0644\u0644\u0651\u064e\u0647\u0650 \u0671\u0644\u0631\u0651\u064e\u062d\u0652\u0645\u064e\u0670\u0646\u0650 \u0671\u0644\u0631\u0651\u064e\u062d\u0650\u064a\u0645\u0650"
                if ar_text.startswith(bismillah):
                    ar_text = ar_text[len(bismillah):].strip()
            
            # Okunan kısmı vurgula
            okunan_kelimeler = item.get("okunan_kelimeler", "")
            arapca_vurgulu = highlight_read_portion(ar_text, okunan_kelimeler) if okunan_kelimeler else ar_text

            main_card = {
                "sure_no": sure_no,
                "ayet_no": ayet_no,
                "arapca": ar_text,
                "arapca_vurgulu": arapca_vurgulu,
                "meal": db_item.get("tr", ""),
                "sure_adi": SURE_ADLARI.get(sure_no, ""),
                "sayfa_no": min(604, db_item.get("page", 1)),
                "sayfa_konum": db_item.get("pos", "orta"),
                "mutesabihler": []  # Düz listeye çevirdiğimiz için nested göstermeyi engelliyoruz
            }
            final_sonuclar.append(main_card)

            # 2. Müteşabih Ayetleri %100 Deterministik Kur'an Veritabanı Araması ile Ekle
            exact_mutashabihs = find_exact_mutashabihat_in_db(okunan_kelimeler, sure_no, ayet_no)
            mutesabihler_raw = [f"{s_no}:{a_no}" for s_no, a_no in exact_mutashabihs]

            for m in mutesabihler_raw:
                m_sure, m_ayet = None, None
                if isinstance(m, str) and ":" in m:
                    try:
                        parts = m.split(":")
                        m_sure = int(parts[0])
                        m_ayet = int(parts[1])
                    except ValueError:
                        continue
                elif isinstance(m, dict):
                    m_sure = m.get("sure_no")
                    m_ayet = m.get("ayet_no")
                
                if m_sure and m_ayet:
                    m_key = f"{m_sure}:{m_ayet}"
                    if m_key in seen_keys:
                        continue
                    seen_keys.add(m_key)
                    m_db = quran_db.get(m_key, {"ar": "", "tr": "", "page": 1, "pos": "orta"})
                    
                    m_ar_text = m_db.get("ar", "")
                    # Fatiha (Sure 1) hariç, 1. ayetlerin başındaki besmeleyi temizle
                    if m_sure != 1 and m_ayet == 1:
                        bismillah = "\u0628\u0650\u0633\u0652\u0645\u0650 \u0671\u0644\u0644\u0651\u064e\u0647\u0650 \u0671\u0644\u0631\u0651\u064e\u062d\u0652\u0645\u064e\u0670\u0646\u0650 \u0671\u0644\u0631\u0651\u064e\u062d\u0650\u064a\u0645\u0650"
                        if m_ar_text.startswith(bismillah):
                            m_ar_text = m_ar_text[len(bismillah):].strip()
                            
                    # Benzer ayetlerde de okunan kısmı vurgula
                    m_arapca_vurgulu = highlight_read_portion(m_ar_text, okunan_kelimeler) if okunan_kelimeler else m_ar_text
                            
                    similar_card = {
                        "sure_no": m_sure,
                        "ayet_no": m_ayet,
                        "arapca": m_ar_text,
                        "arapca_vurgulu": m_arapca_vurgulu,
                        "meal": m_db.get("tr", ""),
                        "sure_adi": SURE_ADLARI.get(m_sure, ""),
                        "sayfa_no": min(604, m_db.get("page", 1)),
                        "sayfa_konum": m_db.get("pos", "orta"),
                        "mutesabihler": []
                    }
                    final_sonuclar.append(similar_card)
        
        return {
            "sonuclar": final_sonuclar, 
            "bulunan_adet": len(final_sonuclar), 
            "limit_bilgisi": limit_bilgisi
        }

    except HTTPException as e: 
        if limit_harcandi and not is_premium:
            limit_iade_et(kullanici_id)
        raise e
    except errors.APIError as e:
        if limit_harcandi and not is_premium:
            limit_iade_et(kullanici_id)
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "503" in err_msg or "UNAVAILABLE" in err_msg:
            raise HTTPException(status_code=503, detail="Google yapay zeka sunucuları şu an çok yoğun. Lütfen birkaç saniye sonra tekrar deneyin.")
        elif "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            raise HTTPException(status_code=429, detail="Yapay zeka istek sınırı aşıldı. Lütfen biraz bekleyip tekrar deneyin.")
        else:
            raise HTTPException(status_code=500, detail=f"Google API Hatası: {err_msg}")
    except Exception as e: 
        if limit_harcandi and not is_premium:
            limit_iade_et(kullanici_id)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/video-izlendi")
async def video_izlendi(x_user_id: str = Header(None, alias="X-User-ID")):
    global kullanici_limitler
    kullanici_id = x_user_id or "anonim"
    bugun = datetime.now().date()
    kayit = kullanici_limitler[kullanici_id]
    
    print(f"--- VIDEO IZLENDI --- User ID: {kullanici_id}")
    print(f"Before: {kayit}")
    
    if kayit["tarih"] != bugun:
        kayit["tarih"] = bugun
        kayit["kullanim"] = 0
    
    if kayit["kullanim"] > 0: 
        kayit["kullanim"] -= 1
        
    print(f"After: {kayit}")
    save_limits(kullanici_limitler)
    return {"basarili": True, "kalan": GUNLUK_LIMIT_UCRETSIZ - kayit["kullanim"]}

@app.post("/test-limit-arttir")
async def test_limit_arttir(x_user_id: str = Header(None, alias="X-User-ID")):
    global kullanici_limitler
    kullanici_id = x_user_id or "anonim"
    kayit = kullanici_limitler[kullanici_id]
    kayit["tarih"] = datetime.now().date()
    kayit["kullanim"] = -8
    save_limits(kullanici_limitler)
    return {"basarili": True, "kalan": 10}

@app.get("/limit-durumu")
async def limit_durumu(x_user_id: str = Header(None, alias="X-User-ID")):
    global kullanici_limitler
    kullanici_id = x_user_id or "anonim"
    bugun = datetime.now().date()
    kayit = kullanici_limitler[kullanici_id]
    
    if kayit["tarih"] != bugun: 
        kalan = GUNLUK_LIMIT_UCRETSIZ
    else: 
        kalan = max(0, GUNLUK_LIMIT_UCRETSIZ - kayit["kullanim"])
        
    return {"kalan": kalan, "limit": GUNLUK_LIMIT_UCRETSIZ}

def temizle_harakat(text: str) -> str:
    # 1. Dagger Alif (superscript Alif \u0670) -> Standart Alif (\u0627) normalizasyonu
    text = text.replace('\u0670', '\u0627')
    # 2. Tashkeel ve tecvid/vakıf işaretlerini temizle
    tashkeel_pattern = re.compile(r'[\u064B-\u065F\u0615-\u061A\u06D6-\u06ED]')
    text = tashkeel_pattern.sub('', text)
    # 3. Elif harflerini normalize et (Vasla, hemzeli elif vb. -> sade elif)
    text = text.replace('\u0622', '\u0627').replace('\u0623', '\u0627').replace('\u0625', '\u0627').replace('\u0671', '\u0627')
    # 4. Te marbuta (ة) -> He (ه) normalizasyonu
    text = text.replace('\u0629', '\u0647')
    # 5. Elif maksure (ى) -> Ya (ي) normalizasyonu
    text = text.replace('\u0649', '\u064A')
    return text

def tr_kucuk(text: str) -> str:
    return text.replace('I', 'ı').replace('İ', 'i').replace('Ğ', 'ğ').replace('Ü', 'ü').replace('Ş', 'ş').replace('Ö', 'ö').replace('Ç', 'ç').lower()

def highlight_arabic_word(ar_text: str, query: str) -> str:
    clean_query = temizle_harakat(query)
    clean_query = "".join(c for c in clean_query if u"\u0621" <= c <= u"\u064A")
    if not clean_query:
        return ar_text
        
    ar_words = ar_text.split()
    best_word_idx = -1
    best_score = 4
    
    for idx, w in enumerate(ar_words):
        cw = temizle_harakat(w)
        cw = "".join(c for c in cw if u"\u0621" <= c <= u"\u064A")
        
        score = 4
        if cw == clean_query:
            score = 1
        elif cw.startswith(clean_query):
            score = 2
        elif clean_query in cw:
            score = 3
            
        if score < best_score:
            best_score = score
            best_word_idx = idx
            
    if best_word_idx != -1:
        target_word = ar_words[best_word_idx]
        ar_words[best_word_idx] = f"<color>{target_word}</color>"
        return " ".join(ar_words)
        
    return ar_text

def highlight_read_portion(ar_text: str, okunan_kelimeler: str) -> str:
    """Sesle okunan kelime grubunu ayet metninde bulup <color> tag ile işaretle.
    Multi-word sliding window ile en iyi eşleşen sürekli aralığı vurgular."""
    if not okunan_kelimeler or not ar_text:
        return ar_text

    def norm(t):
        # Harekesiz, sade harf normalize
        t = temizle_harakat(t)
        return "".join(c for c in t if '\u0600' <= c <= '\u06FF')

    ar_words = ar_text.split()
    ok_words = okunan_kelimeler.strip().split()
    if not ok_words:
        return ar_text

    norm_ar = [norm(w) for w in ar_words]
    norm_ok = [norm(w) for w in ok_words]
    
    # Remove empty normalized tokens
    norm_ok = [w for w in norm_ok if w]
    if not norm_ok:
        return ar_text

    n = len(norm_ar)
    k = len(norm_ok)

    best_start = -1
    best_end = -1
    best_score = -1

    # Sliding window of size k over the ayet words
    for start in range(n):
        window = norm_ar[start:start + k]
        if not window:
            break
        # Count matching words (allowing partial prefix match)
        matched = 0
        for wa, wo in zip(window, norm_ok):
            if wa == wo or wa.startswith(wo) or wo.startswith(wa):
                matched += 1
        score = matched
        end = min(start + k - 1, n - 1)
        if score > best_score:
            best_score = score
            best_start = start
            best_end = end

    # Only highlight if at least half the read words matched
    if best_start != -1 and best_score >= max(1, len(norm_ok) // 2):
        highlighted = (
            ar_words[:best_start]
            + ["<color>" + " ".join(ar_words[best_start:best_end + 1]) + "</color>"]
            + ar_words[best_end + 1:]
        )
        return " ".join(highlighted)

    return ar_text

def highlight_turkish_word(text: str, query: str) -> str:
    q_clean = tr_kucuk(query)
    t_clean = tr_kucuk(text)
    start_idx = t_clean.find(q_clean)
    if start_idx == -1:
        return text
    end_idx = start_idx + len(q_clean)
    matched_orig = text[start_idx:end_idx]
    return text[:start_idx] + f"<color>{matched_orig}</color>" + text[end_idx:]

@app.get("/sure-ayetleri")
async def sure_ayetleri(sure_no: int):
    results = []
    for key, v in quran_db.items():
        s_no, a_no = map(int, key.split(":"))
        if s_no == sure_no:
            results.append({
                "ayet_no": a_no,
                "arapca": v["ar"],
                "meal": v["tr"],
                "sayfa_no": v["page"]
            })
    results.sort(key=lambda x: x["ayet_no"])
    return {"ayetler": results}

@app.get("/ayet-meal")
async def ayet_meal(sure_no: int, ayet_no: int):
    key = f"{sure_no}:{ayet_no}"
    if key in quran_db:
        return {"meal": quran_db[key]["tr"], "arapca": quran_db[key]["ar"]}
    return {"meal": "Meal bulunamadı."}

@app.get("/sozluk-oner")
async def sozluk_oner(q: str):
    q_clean = temizle_harakat(q).strip()
    if not q_clean:
        return {"oneriler": []}
    
    matches = set()
    for key, v in quran_db.items():
        words = v["ar"].split()
        for w in words:
            w_clean = temizle_harakat(w)
            w_display = "".join([c for c in w if c not in ["﴿", "﴾", "ۖ", "ۗ", "ۚ", "ۛ", "۩", " ", "◌", "۞", "۩"]])
            w_display_clean = temizle_harakat(w_display)
            if q_clean in w_display_clean:
                matches.add(w_display.strip())
                if len(matches) >= 30:
                    break
        if len(matches) >= 30:
            break
            
    match_list = list(matches)
    def sort_key(word):
        w_c = temizle_harakat(word)
        starts = w_c.startswith(q_clean)
        length = len(w_c)
        return (not starts, length)
        
    match_list.sort(key=sort_key)
    return {"oneriler": match_list[:12]}

@app.get("/sozluk-ara")
async def sozluk_ara(q: str = Query(..., min_length=1)):
    results = []
    query = q.strip()
    if not query:
        return {"sonuclar": [], "adet": 0}
        
    def calculate_match_score(text: str, q_val: str) -> int:
        norm_text = temizle_harakat(text)
        norm_query = temizle_harakat(q_val)
        
        raw_words = norm_text.split()
        clean_words = []
        for w in raw_words:
            cw = "".join(c for c in w if u"\u0621" <= c <= u"\u064A")
            if cw:
                clean_words.append(cw)
                
        clean_q = "".join(c for c in norm_query if u"\u0621" <= c <= u"\u064A")
        if not clean_q:
            return 4
            
        clean_text_joined = " ".join(clean_words)
        if clean_q == clean_text_joined:
            return 1
            
        if clean_q in clean_words:
            return 1
            
        for w in clean_words:
            if w.startswith(clean_q):
                return 2
                
        if clean_q in clean_text_joined:
            return 3
            
        return 4

    normalized_query = temizle_harakat(query)
    
    for key, v in quran_db.items():
        sure_no, ayet_no = map(int, key.split(":"))
        ar_text = v["ar"]
        tr_text = v["tr"]
        
        normalized_ar = temizle_harakat(ar_text)
        if normalized_query in normalized_ar:
            clean_ar = ar_text
            if sure_no != 1 and ayet_no == 1:
                bismillah = "\u0628\u0650\u0633\u0652\u0645\u0650 \u0671\u0644\u0644\u0651\u064e\u0647\u0650 \u0671\u0644\u0631\u0651\u064e\u062d\u0652\u0645\u064e\u0670\u0646\u0650 \u0671\u0644\u0631\u0651\u064e\u062d\u0650\u064a\u0645\u0650"
                if clean_ar.startswith(bismillah):
                    clean_ar = clean_ar[len(bismillah):].strip()
            
            highlighted_ar = highlight_arabic_word(clean_ar, query)
            score = calculate_match_score(clean_ar, query)
            results.append({
                "sure_no": sure_no,
                "ayet_no": ayet_no,
                "sure_adi": SURE_ADLARI.get(sure_no, ""),
                "arapca": highlighted_ar,
                "meal": tr_text,
                "sayfa_no": v["page"],
                "score": score
            })
            
    # Sort by exactness match score
    results.sort(key=lambda x: x.get("score", 3))
    for r in results:
        r.pop("score", None)
        
    return {"sonuclar": results[:50], "adet": len(results[:50])}

@app.post("/ai-rehberlik")
async def ai_rehberlik(data: dict):
    topic = data.get("topic", "")
    if not topic:
        raise HTTPException(status_code=400, detail="Konu boş olamaz")
    
    system_prompt = "Sen 'Hafız AI' isminde akıllı bir manevi asistansın. Kullanıcıya Kur'an'dan ayetlerle, tefsirle ve rehberlikle Türkçe dilde çok kibar, sakinleştirici ve nurlu bir dille cevap verirsin."
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=topic,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt
            )
        )
        return {"response": response.text}
    except Exception as e:
        return {"response": f"Hata oluştu: {str(e)}"}

@app.post("/ai-kelime-koku")
async def ai_kelime_koku(data: dict):
    word = data.get("word", "")
    transliteration = data.get("transliteration", "")
    root = data.get("root", "")
    count = data.get("count", 0)
    forms = data.get("forms", "")
    
    system_prompt = "Sen bir İslam alimi ve Kur'an kelime bilimcisisin. Kelimelerin köklerini (Arapça) ve Kur'an'daki derin teolojik/tasavvufi manalarını Türkçe dilinde kısa, veciz ve çok edebi bir üslupla açıklarsın."
    user_prompt = f"""
    Kur'an'da geçen '{word}' ({transliteration}) kelimesini incele.
    Bu kelimenin kökü '{root}' köküdür. Kur'an'da tam {count} kez geçmektedir ve {forms} farklı kalıpta türemiştir.
    Bize bu kelimenin kök anlamını ve Kur'an-ı Kerim'de ifade ettiği derin anlamları, manevi/kalbi boyutlarıyla 3 kısa madde halinde açıklar mısın?
    """
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt
            )
        )
        return {"response": response.text}
    except Exception as e:
        return {"response": f"Hata oluştu: {str(e)}"}
