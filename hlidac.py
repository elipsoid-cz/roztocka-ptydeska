#!/usr/bin/env python3
"""
Hlídač veřejných projednání územně plánovací dokumentace.

Stáhne nové dokumenty z úředních desek přes API edesky.cz, vyfiltruje ty,
které se týkají územního plánování, nechá je Gemini přeložit do lidské češtiny
a doručí výsledek: založí GitHub issue (o kterém GitHub pošle e-mail) a
volitelně pošle zprávu na Telegram.

Spouští se z GitHub Actions jednou denně. Stav se drží v state/ a commituje
zpátky do repozitáře: seen.json jsou už viděné dokumenty, zpravy.json je
archiv odeslaných zpráv, ze kterého čte stránka index.html.

Proměnné prostředí:
  EDESKY_API_KEY      povinné, klíč z profilu na edesky.cz
  GEMINI_API_KEY      povinné, klíč z aistudio.google.com/api-keys
  GITHUB_TOKEN        volitelné, bez něj se nezakládají issues
  GITHUB_REPOSITORY   volitelné, "vlastnik/repo", doplňuje GitHub Actions samo
  TELEGRAM_BOT_TOKEN  volitelné, bez něj se na Telegram neposílá
  TELEGRAM_CHAT_ID    volitelné
  EDESKY_DASHBOARDS   volitelné, čárkou oddělená ID desek (default 260 = Roztoky)
  LOOKBACK_DAYS       volitelné, kolik dní zpět se ptát (default 7)
  GEMINI_MODEL        volitelné (default gemini-3.5-flash-lite)
  DRY_RUN             volitelné, "1" = neukládat stav a neodesílat

Speciální režim:
  python hlidac.py --najdi-desku "Černošice"
      vypíše ID úředních desek, jejichž název obsahuje daný řetězec
"""

import json
import os
import pathlib
import random
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests

# --------------------------------------------------------------------------
# Konfigurace
# --------------------------------------------------------------------------

EDESKY_API = "https://edesky.cz/api/v1"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"

EDESKY_API_KEY = os.environ.get("EDESKY_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")

DASHBOARDS = [d.strip() for d in os.environ.get("EDESKY_DASHBOARDS", "260").split(",") if d.strip()]
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

STATE_FILE = pathlib.Path("state/seen.json")

# Archiv odeslaných zpráv. Čte ho index.html, takže formát je zároveň
# veřejné API stránky — když se do záznamu přidá pole, přidej ho i tam.
ARCHIV_FILE = pathlib.Path("state/zpravy.json")

# Kolik zpráv v archivu držet. Stránka je čte všechny najednou.
MAX_ARCHIV = 500

# Kolik znaků textu dokumentu posílat do modelu.
# Záměrně málo: podstatné (co, kde, kdy, do kdy) bývá na první stránce,
# zatímco rozdělovník s desítkami jmen a adres je až na konci. Tím se
# osobní údaje účastníků řízení do modelu většinou vůbec nedostanou.
MAX_TEXT_CHARS = 6000

# Hledané výrazy. Fulltext (search_with=es) prohledává i naOCRovaný obsah PDF,
# takže projednání schované pod obecným názvem neuteče.
KEYWORDS = [
    "veřejné projednání",
    "opakované veřejné projednání",
    "společné jednání",
    "regulační plán",
    "územní plán",
    "územní studie",
    "opatření obecné povahy",
    "stavební uzávěra",
    # lokality specifické pro Roztoky
    "Panenská",
    "Dubečnice",
    "Solníky",
    "Tiché údolí",
    "Žalov",
]

PROMPT = """Jsi asistent, který pomáhá obyvatelům města porozumět úředním \
vyhláškám o územním plánování.

Dostaneš text dokumentu z úřední desky. Vyhodnoť ho a odpověz v JSON.

Nejdřív rozhodni, zda dokument opravdu souvisí s pořizováním nebo změnou \
územně plánovací dokumentace (územní plán, regulační plán, územní studie, \
územní opatření o stavební uzávěře, vymezení zastavěného území) — typicky \
oznámení o společném jednání, veřejném projednání, o vydání nebo o zahájení \
pořizování. Pokud jde o něco jiného (odstávka elektřiny, dražba, výběrové \
řízení, uzavírka silnice, běžné územní nebo stavební řízení o jedné konkrétní \
stavbě, oznámení o uložení písemnosti), nastav "relevantni" na false a ostatní \
pole nech prázdná.

Pokud relevantní je, vyplň zbytek. Piš česky, srozumitelně, bez úřednického \
jazyka, tak aby tomu rozuměl člověk bez právního vzdělání. Nevymýšlej si — co \
v textu není, nech prázdné. Datum piš ve formátu DD.MM.RRRR.

Text dokumentu:
---
{text}
---
"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "relevantni": {
            "type": "BOOLEAN",
            "description": "Týká se dokument územně plánovací dokumentace?",
        },
        "nadpis": {
            "type": "STRING",
            "description": "Krátký srozumitelný název, max 10 slov.",
        },
        "lokalita": {
            "type": "STRING",
            "description": "Kterých míst, ulic nebo lokalit se to týká.",
        },
        "datum_jednani": {
            "type": "STRING",
            "description": "Datum a čas veřejného projednání, prázdné pokud není.",
        },
        "misto_jednani": {
            "type": "STRING",
            "description": "Kde se jednání koná.",
        },
        "deadline": {
            "type": "STRING",
            "description": "Do kdy lze podat námitky nebo připomínky.",
        },
        "kdo_muze_podat": {
            "type": "STRING",
            "description": "Kdo je oprávněn podat námitku a kdo jen připomínku.",
        },
        "shrnuti": {
            "type": "STRING",
            "description": "3 až 5 vět o tom, co se navrhuje a co to znamená.",
        },
    },
    "required": ["relevantni", "nadpis", "shrnuti"],
}


# --------------------------------------------------------------------------
# edesky.cz
# --------------------------------------------------------------------------

def edesky_get(endpoint: str, params: dict) -> ET.Element:
    """Zavolá edesky API a vrátí kořen XML."""
    params = dict(params, api_key=EDESKY_API_KEY)
    resp = requests.get(
        f"{EDESKY_API}/{endpoint}",
        params=params,
        timeout=60,
        headers={"User-Agent": "hlidac-uredni-desky/1.0 (obcanska iniciativa)"},
    )
    resp.raise_for_status()
    if "<edesky_search_api" not in resp.text:
        # Typicky Anubis nebo jiná ochrana místo očekávaného XML.
        raise RuntimeError(
            f"edesky nevrátilo XML (HTTP {resp.status_code}). "
            f"Začátek odpovědi: {resp.text[:200]!r}"
        )
    return ET.fromstring(resp.text)


def najdi_desku(potreba: str) -> None:
    """Vypíše ID desek, jejichž název obsahuje hledaný řetězec."""
    root = edesky_get("dashboards", {"include_subordinated": 1})
    potreba_low = potreba.lower()
    nalezeno = False
    for d in root.iter("dashboard"):
        name = d.get("name", "")
        if potreba_low in name.lower():
            nalezeno = True
            print(f"{d.get('edesky_id'):>8}  {name}  ({d.get('edesky_url')})")
    if not nalezeno:
        print(f"Nic pro '{potreba}'. Zkus kratší nebo jinak napsaný výraz.")


def stahni_dokumenty() -> dict:
    """Vrátí dict {edesky_url: dokument} napříč všemi deskami a klíčovými slovy."""
    od = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    nalezene = {}

    for dashboard_id in DASHBOARDS:
        for kw in KEYWORDS:
            try:
                root = edesky_get(
                    "documents",
                    {
                        "keywords": kw,
                        "search_with": "es",   # fulltext včetně obsahu PDF
                        "dashboard_id": dashboard_id,
                        "created_from": od,
                        "include_texts": 1,
                        "order": "date",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! deska {dashboard_id}, '{kw}': {exc}", file=sys.stderr)
                continue

            for doc in root.iter("document"):
                url = doc.get("edesky_url")
                if not url or url in nalezene:
                    continue
                nalezene[url] = {
                    "url": url,
                    "nazev": doc.get("name", "").strip(),
                    "deska": doc.get("dashboard_name", ""),
                    "vlozeno": doc.get("created_at", ""),
                    "orig_url": platne_url(doc.get("orig_url", "")),
                    "text": vytahni_text(doc),
                }
            time.sleep(1)  # slušnost vůči cizímu API

    return nalezene


def platne_url(u: str) -> str:
    """
    Vrátí adresu, jen když je to opravdu http(s) odkaz, jinak prázdný řetězec.

    edesky přebírá orig_url z webů úřadů a občas tam místo adresy přistane
    poznámka správce — viděli jsme "#_pokud-potrebujete-kontaktujte_...#".
    Takový řetězec nemá co dělat v odkazu: ve zprávě je z něj rozbitý link
    a na stránce je to cizí text vkládaný do href.
    """
    u = (u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def vytahni_text(doc: ET.Element) -> str:
    """
    Poskládá text ze všech příloh dokumentu.

    edesky vrací text uvnitř <attachment> percent-encoded, proto unquote.
    """
    kusy = []
    for att in doc.iter("attachment"):
        if att.get("contains_text") != "1" or not att.text:
            continue
        try:
            kusy.append(urllib.parse.unquote(att.text.strip()))
        except Exception:  # noqa: BLE001
            kusy.append(att.text.strip())
    return "\n\n".join(kusy).strip()


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

def prelozi_do_lidstiny(dok: dict) -> dict | None:
    """Pošle text do Gemini a vrátí strukturované shrnutí, nebo None."""
    text = (dok["nazev"] + "\n\n" + dok["text"])[:MAX_TEXT_CHARS]
    if len(text.strip()) < 50:
        # Nepovedené OCR nebo prázdná příloha — nemá cenu utrácet volání.
        return None

    payload = {
        "contents": [{"parts": [{"text": PROMPT.format(text=text)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0.2,
        },
    }

    url = f"{GEMINI_API}/{GEMINI_MODEL}:generateContent"
    for pokus in range(5):
        resp = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=120,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            # Free tier má nízké limity, tak počkáme a zkusíme znovu.
            cekej = (2 ** pokus) + random.random()
            print(f"  … HTTP {resp.status_code}, čekám {cekej:.1f}s", file=sys.stderr)
            time.sleep(cekej)
            continue
        resp.raise_for_status()
        data = resp.json()
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            print(f"  ! nečekaná odpověď Gemini: {exc}", file=sys.stderr)
            return None

    print("  ! Gemini se nepodařilo zavolat ani na pátý pokus", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Výstup
# --------------------------------------------------------------------------

def nadpis(dok: dict, shrnuti: dict) -> str:
    return shrnuti.get("nadpis") or dok["nazev"]


def casti(shrnuti: dict) -> list[tuple[str, str, bool]]:
    """
    Rozpadne zprávu na řádky (emoji, text, tučně).

    Renderery níž z toho složí HTML pro Telegram nebo Markdown pro issue.
    Díky společnému základu se obě podoby nerozejdou, až se bude přidávat
    další pole.
    """
    r = []
    if shrnuti.get("lokalita"):
        r.append(("📍", shrnuti["lokalita"], False))
    if shrnuti.get("datum_jednani"):
        misto = shrnuti.get("misto_jednani")
        text = f"Projednání: {shrnuti['datum_jednani']}" + (f", {misto}" if misto else "")
        r.append(("🗓", text, True))
    if shrnuti.get("deadline"):
        r.append(("⏳", f"Námitky do: {shrnuti['deadline']}", True))
    if shrnuti.get("kdo_muze_podat"):
        r.append(("👤", shrnuti["kdo_muze_podat"], False))
    return r


def esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_md(s: str) -> str:
    """Neutralizuje znaky, kterými by úřední text omylem rozbil Markdown."""
    for zn in "\\`*_[]<>":
        s = (s or "").replace(zn, "\\" + zn)
    return s


def zprava_html(dok: dict, shrnuti: dict) -> str:
    """Podoba pro Telegram (parse_mode=HTML)."""
    r = [f"<b>{esc_html(nadpis(dok, shrnuti))}</b>"]
    for emoji, text, tucne in casti(shrnuti):
        t = esc_html(text)
        r.append(f"{emoji} " + (f"<b>{t}</b>" if tucne else t))
    r.append("")
    r.append(esc_html(shrnuti.get("shrnuti", "")))
    r.append("")
    r.append(f"<i>{esc_html(dok['deska'])}, vyvěšeno {esc_html(dok['vlozeno'][:10])}</i>")
    r.append(f'<a href="{dok["url"]}">dokument na edesky</a>')
    if dok.get("orig_url"):
        r.append(f'<a href="{dok["orig_url"]}">originál na úřední desce</a>')
    return "\n".join(r)


def zprava_md(dok: dict, shrnuti: dict) -> str:
    """
    Podoba pro GitHub issue. Vypisuje se i do logu běhu, proto se musí dát
    přečíst i nevykreslená.
    """
    r = []
    for emoji, text, tucne in casti(shrnuti):
        t = esc_md(text)
        r.append(f"{emoji} " + (f"**{t}**" if tucne else t))
    r.append("")
    r.append(esc_md(shrnuti.get("shrnuti", "")))
    r.append("")
    r.append(f"[dokument na edesky]({dok['url']})")
    if dok.get("orig_url"):
        r.append(f"[originál na úřední desce]({dok['orig_url']})")
    r.append("")
    r.append(f"*{esc_md(dok['deska'])}, vyvěšeno {esc_md(dok['vlozeno'][:10])}*")
    return "\n".join(r)


def posli(dok: dict, shrnuti: dict) -> list[str]:
    """
    Vypíše zprávu do logu a mimo dry run ji doručí.

    Vrací seznam kanálů, kterými se to povedlo — jde do archivu, ať je na
    stránce poznat, jestli zpráva opravdu někam dorazila.
    """
    telo = zprava_md(dok, shrnuti)
    print("\n" + "=" * 60)
    print(nadpis(dok, shrnuti))
    print(telo)

    if DRY_RUN:
        print("\n  (dry run, neodesílám)")
        return []

    doruceno = []
    if posli_issue(nadpis(dok, shrnuti), telo):
        doruceno.append("issue")
    if posli_telegram(zprava_html(dok, shrnuti)):
        doruceno.append("telegram")
    return doruceno


def posli_issue(titulek: str, telo: str) -> bool:
    """
    Založí issue v repozitáři. E-mail z něj rozešle GitHub sám všem, kdo
    repozitář sledují — proto tudy a ne přes SMTP: není co konfigurovat
    a není heslo, které by za rok vypršelo.
    """
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        print("  (bez GITHUB_TOKEN, issue nezakládám)", file=sys.stderr)
        return False
    resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"title": titulek, "body": telo},
        timeout=30,
    )
    if not resp.ok:
        print(f"  ! GitHub issue: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return False
    return True


def posli_telegram(text: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"  ! Telegram: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------
# Stav
# --------------------------------------------------------------------------

def nacti_stav() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return set()


def uloz_stav(videne: set) -> None:
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Držíme jen posledních 2000 záznamů, ať soubor neroste donekonečna.
    STATE_FILE.write_text(
        json.dumps(sorted(videne)[-2000:], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def nacti_archiv() -> list:
    if ARCHIV_FILE.exists():
        return json.loads(ARCHIV_FILE.read_text(encoding="utf-8"))
    return []


def uloz_archiv(archiv: list) -> None:
    if DRY_RUN:
        return
    ARCHIV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIV_FILE.write_text(
        json.dumps(archiv[:MAX_ARCHIV], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def zaznam(dok: dict, shrnuti: dict, doruceno: list[str]) -> dict:
    """Jeden záznam do archivu. Tenhle tvar čte index.html."""
    return {
        "cas": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nadpis": nadpis(dok, shrnuti),
        "lokalita": shrnuti.get("lokalita", ""),
        "datum_jednani": shrnuti.get("datum_jednani", ""),
        "misto_jednani": shrnuti.get("misto_jednani", ""),
        "deadline": shrnuti.get("deadline", ""),
        "kdo_muze_podat": shrnuti.get("kdo_muze_podat", ""),
        "shrnuti": shrnuti.get("shrnuti", ""),
        "nazev_dokumentu": dok["nazev"],
        "deska": dok["deska"],
        "vlozeno": dok["vlozeno"][:10],
        "url": dok["url"],
        "orig_url": dok.get("orig_url", ""),
        "doruceno": doruceno,
    }


# --------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--najdi-desku":
        najdi_desku(sys.argv[2])
        return 0

    for jmeno in ("EDESKY_API_KEY", "GEMINI_API_KEY"):
        if not os.environ.get(jmeno):
            print(f"Chybí proměnná {jmeno}", file=sys.stderr)
            return 2

    videne = nacti_stav()
    archiv = nacti_archiv()
    print(f"Znám {len(videne)} dokumentů, dívám se {LOOKBACK_DAYS} dní zpět "
          f"na desky {', '.join(DASHBOARDS)}.")
    if DRY_RUN:
        print("Dry run: stav ani archiv se neuloží a nic se neodešle.")

    dokumenty = stahni_dokumenty()
    nove = {u: d for u, d in dokumenty.items() if u not in videne}
    print(f"Nalezeno {len(dokumenty)} dokumentů, z toho {len(nove)} nových.")

    poslano = 0
    for url, dok in nove.items():
        print(f"\n→ {dok['nazev'][:80]}")
        shrnuti = prelozi_do_lidstiny(dok)
        videne.add(url)

        if shrnuti is None:
            print("  (nepodařilo se zpracovat, příště se na něj podívám znovu)")
            videne.discard(url)
            continue
        if not shrnuti.get("relevantni"):
            print("  (netýká se územního plánování, přeskakuji)")
            continue

        doruceno = posli(dok, shrnuti)
        # Nejnovější nahoru, stránka to tak čte bez dalšího řazení.
        archiv.insert(0, zaznam(dok, shrnuti, doruceno))
        poslano += 1
        time.sleep(5)  # ať se nepereme s limitem free tieru

    uloz_stav(videne)
    uloz_archiv(archiv)
    print(f"\nHotovo. Zpráv k odeslání: {poslano}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
