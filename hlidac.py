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
import re
import sys
import time
import unicodedata
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

# Náhledový režim: projde i dokumenty, které už zná, nechá je přeložit
# a výsledek uloží stranou do state/nahled.json, ze kterého čte stránka
# na index.html?zdroj=nahled. Nic neodesílá a nesahá na ostrý stav —
# je to dílna na posuzování kvality překladu, ne druhý hlídač.
NAHLED = os.environ.get("NAHLED") == "1"

# Strop na počet dokumentů v náhledu. Chrání free tier Gemini před tím,
# aby roční zpětný pohled spotřeboval denní kvótu.
NAHLED_MAX = int(os.environ.get("NAHLED_MAX", "25"))

STATE_FILE = pathlib.Path("state/seen.json")

# Dokumenty, na které jsme upozornili holým nálezem, protože edesky nemá
# rozpoznaný text. Schválně mimo seen.json: až text přibude, chceme je
# zpracovat pořádně a poslat znovu i se shrnutím.
BEZ_TEXTU_FILE = pathlib.Path("state/bez_textu.json")

# Archiv odeslaných zpráv. Čte ho index.html, takže formát je zároveň
# veřejné API stránky — když se do záznamu přidá pole, přidej ho i tam.
ARCHIV_FILE = pathlib.Path("state/zpravy.json")

# Výstup náhledového režimu. Schválně jiný soubor než archiv: nesmí se stát,
# že si někdo splete zkušební překlad s tím, co hlídač opravdu poslal.
NAHLED_FILE = pathlib.Path("state/nahled.json")

# Holý soupis všeho, co za sledované období viselo na deskách — bez ohledu
# na klíčová slova a bez textů. Slouží jen stránce, aby šlo dohledat i to,
# co hlídač neposlal. Do modelu z něj nejde nic.
DESKA_FILE = pathlib.Path("state/deska.json")

# Jak hluboko do minulosti soupis stahovat. Schválně nezávisle na
# LOOKBACK_DAYS: nálezy stačí hledat za posledních pár dní, ale soupis má
# na stránce dávat souvislost i o kus zpátky. Starší záznamy v souboru
# zůstávají, každý běh je jen doplní.
DESKA_STAHOVAT_DNI = int(os.environ.get("DESKA_STAHOVAT_DNI", "30"))

# Jak dlouho záznamy v soupisu držet. Delší paměť nemá cenu: kdo hledá
# loňský dokument, jde na edesky, ne sem.
DESKA_DRZET_DNI = int(os.environ.get("DESKA_DRZET_DNI", "180"))

# Strop na stránkování soupisu (edesky vrací 200 dokumentů na stránku).
SOUPIS_MAX_STRANEK = 10

# Kolik zpráv v archivu držet. Stránka je čte všechny najednou.
MAX_ARCHIV = 500

# Kolik znaků vlastního textu dokumentu musí být k dispozici, aby mělo smysl
# ptát se modelu na shrnutí. Pod tím edesky nemá rozpoznaný text (naskenované
# PDF bez OCR, nebo ho ještě nestihlo zpracovat) a model by shrnutí vymyslel
# ze samotného názvu — což se přesně jednou stalo a je to horší než mlčet.
MIN_TEXT_CHARS = 200

# Seznam ulic Roztok a Žalova z RÚIAN. Posílá se modelu, aby ulice psal
# kanonicky, a hlavně slouží k tomu, aby se z jeho odpovědi vyhodily ulice,
# které v Roztokách neexistují. Ruční doplňování zakázáno: soubor se obnovuje
# stažením z ČÚZK, adresa je uvnitř.
MISTOPIS_FILE = pathlib.Path("mistopis.json")

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

# Výrazy, které samotné v názvu dokumentu stačí na upozornění, i když k němu
# nemáme text. Schválně užší než KEYWORDS — chybí „opatření obecné povahy"
# a názvy lokalit, pod které spadne i uzavírka silnice. Bez textu totiž nemá
# kdo posoudit relevanci a falešný poplach je tu dražší než jinde.
RE_SILNE = re.compile(
    r"regulačn\w*\s+plán\w*"
    r"|územn\w*\s+plán\w*"
    r"|územn\w*\s+studi\w*"
    r"|stavebn\w*\s+uzávě[rř]\w*"   # pozor: „o stavební uzávěře" je s ř
    r"|veřejn\w*\s+projednán\w*"
    r"|společn\w*\s+jednán\w*",
    re.IGNORECASE,
)

PROMPT = """Jsi redaktor, který obyvatelům Roztok a Žalova překládá dokumenty \
z úřední desky do srozumitelné češtiny. Nejsi právník ani mluvčí úřadu, jsi \
soused, který ostatním vysvětlí, co pro ně ta vyhláška znamená.

Dostaneš text dokumentu z úřední desky. Odpověz v JSON podle schématu. Piš \
česky, krátkými větami, v přítomném čase, bez úřednických obratů. Pomlčka je \
vždycky „–", nikdy „—".

KROK 1, RELEVANCE

Rozhodni, jestli dokument souvisí s pořizováním nebo změnou územně plánovací \
dokumentace (územní plán, regulační plán, územní studie, územní opatření o \
stavební uzávěře, vymezení zastavěného území), typicky oznámení o společném \
jednání, veřejném projednání, o vydání nebo o zahájení pořizování. Pokud jde o \
něco jiného (odstávka elektřiny, dražba, výběrové řízení, uzavírka silnice, \
volby, běžné územní nebo stavební řízení o jedné konkrétní stavbě, oznámení o \
uložení písemnosti), nastav „relevantni" na false a ostatní pole nech prázdná.

KROK 2, MÍSTO

„lokalita" je místní název, pod kterým to lidé znají: Solníky, Panenská II, \
Dubečnice, Tiché údolí, Žalov. Uveď ho, jen když v dokumentu opravdu je.

„ulice" jsou ulice, kde se něco mění nebo kde bude něco platit. Nepatří sem \
ulice, které jsou v textu jen jako orientační bod: z věty „naproti křížení s \
ulicí Braunerova" se mění něco jinde, ne v Braunerově. Piš je v prvním pádě a \
přesně v té podobě, v jaké stojí v tomhle seznamu ulic Roztok a Žalova:

{ulice}

Ulici, která v dokumentu není, nikdy nedoplňuj. Když dokument žádnou neuvádí, \
nech pole prázdné. U územně plánovací dokumentace je to běžné, protože \
vymezení bývá jen ve výkresech.

KROK 3, NADPIS

Nadpis říká, CO se děje a KDE. Nejvýš 12 slov. Nikdy v něm není typ dokumentu \
(veřejná vyhláška, oznámení, opatření obecné povahy, návrh), číslo jednací ani \
paragrafy. Od toho jsou jiná pole.

Dobře: „Změna pravidel zástavby v Solníkách, projednání 29. 9."
Špatně: „Veřejná vyhláška – oznámení o návrhu opatření obecné povahy"
Špatně: „Návrh změny č. 1 regulačního plánu Solníky" (jen opsaný název)

KROK 4, SHRNUTÍ

Tři až čtyři věty, nejvýš 90 slov, v tomhle pořadí:

1. Co konkrétně se mění nebo děje, i s místem. Pojmy přelož na význam: \
regulační plán jsou podrobná pravidla, co a jak se smí v dané lokalitě \
stavět; územní plán je základní plán, kde smí být domy, zeleň a průmysl; \
územní studie je podklad, který prověřuje, jak by se lokalita dala uspořádat; \
stavební uzávěra znamená, že se tam zatím nesmí stavět.
2. Koho se to týká, tedy kde bydlí nebo co vlastní.
3. Co s tím může udělat a do kdy. Rozliš připomínku, kterou může podat \
kdokoli, od námitky, kterou smí podat jen dotčení vlastníci. Když dokument \
říká, že námitky podat nelze, napiš to.
4. Nepovinně kontext, který dokument sám uvádí a laikovi pomůže: kdo o změnu \
požádal a proč.

ZAKÁZANÉ VĚTY. Tyhle věty neplatí o ničem konkrétním, protože sedí na každou \
vyhlášku. Nepiš je ani jinými slovy:
– „mění dosavadní podmínky v území"
– „může ovlivnit budoucí podobu a rozvoj lokality"
– „obyvatelé se mohou seznámit s dokumentací"
– „dokumentace je k nahlédnutí na úřadě nebo na webu města"
– „veřejnost se může vyjádřit v zákonné lhůtě"
– „úřad vydal opatření obecné povahy"

KDYŽ PODSTATA CHYBÍ. Když v textu není, co konkrétně se v území mění, a \
dokument jen odkazuje na výkresy, na přílohy nebo na web města, nastav \
„podstata_nalezena" na false a napiš to rovnou jako první větu shrnutí, \
například: „Co přesně se v Solníkách změní, vyhláška neuvádí, podrobnosti \
jsou jen v textové části a ve výkresech na webu města." Zbytek shrnutí, tedy \
koho se to týká a do kdy se dá reagovat, napiš normálně, ty údaje ve vyhlášce \
jsou. Obsah si nedomýšlej. Přiznat, že podstata chybí, je užitečná informace, \
obecná věta není nic. Když v textu naopak je, co se mění, nastav \
„podstata_nalezena" na true.

LHŮTY A DATA jsou to nejdůležitější, kvůli čemu tuhle zprávu někdo dostane.
– Absolutní datum piš ve formátu DD.MM.RRRR.
– Lhůty v těchhle vyhláškách bývají popsané vztahem k události, ne datem, \
například „námitky lze podat nejpozději do 7 dnů ode dne veřejného \
projednání" nebo „do 30 dnů ode dne doručení". Takovou lhůtu opiš přesně tak, \
jak je v dokumentu. Pole „deadline" NENECHÁVEJ prázdné jen proto, že tam není \
konkrétní datum.
– Když je v dokumentu i datum události, od které se lhůta počítá, dopočítej \
výsledné datum a napiš obojí, třeba „09.09.2026 (7 dnů od veřejného \
projednání)".
– Lhůtu si nikdy nedopočítávej z paragrafů ani z obecných pravidel o \
doručování. Když v dokumentu žádná lhůta není, nech pole prázdné.

KONTROLA PŘED ODESLÁNÍM
– Je v nadpisu místo nebo lokalita, když ji dokument uvádí?
– Není ve shrnutí zakázaná věta nebo zkratka bez vysvětlení?
– Je ze shrnutí hned jasné, jestli se s tím dá ještě něco dělat?
– Jsou všechny ulice, lokality a data doslova v dokumentu?

Dokument byl vyvěšen na úřední desce {vyveseno}.

Text dokumentu:
---
{text}
---
"""

# Diagnostika pro dry run. Zajímá nás, jestli model pole minul, nebo v textu
# opravdu není — vypisujeme jen nalezené vzory, ne text dokumentu, protože
# log běhu je ve veřejném repozitáři.
RE_DATUM = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?\d{4}\b")
RE_LHUTA = re.compile(r"(?:nejpozději|ve lhůtě|do)\s+\d+\s+(?:dn|kalendářních dn)\w*", re.I)

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
            "description": (
                "Místní název, pod kterým lokalitu znají místní (Solníky, "
                "Panenská II, Tiché údolí, Žalov). Prázdné, když ho dokument "
                "neuvádí."
            ),
        },
        "ulice": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": (
                "Ulice, kde se něco mění nebo kde bude něco platit, v prvním "
                "pádě a v podobě ze seznamu ulic. Ne ulice zmíněné jen jako "
                "orientační bod. Jen ty, které v dokumentu doslova jsou."
            ),
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
            "description": (
                "Do kdy lze podat námitky nebo připomínky. Buď datum DD.MM.RRRR, "
                "nebo lhůta popsaná vztahem k události tak, jak stojí v dokumentu "
                "(např. „do 7 dnů ode dne veřejného projednání\"). Prázdné jen "
                "tehdy, když v dokumentu žádná lhůta není."
            ),
        },
        "kdo_muze_podat": {
            "type": "STRING",
            "description": "Kdo je oprávněn podat námitku a kdo jen připomínku.",
        },
        "shrnuti": {
            "type": "STRING",
            "description": (
                "3 až 4 věty: co konkrétně se mění a kde, koho se to týká, co "
                "s tím může udělat a do kdy. Bez obecných frází, které platí "
                "o každé vyhlášce."
            ),
        },
        "podstata_nalezena": {
            "type": "BOOLEAN",
            "description": (
                "Je v textu, co konkrétně se v území mění? False, když "
                "dokument jen odkazuje na výkresy, přílohy nebo na web města."
            ),
        },
    },
    # podstata_nalezena je povinná schválně: chybějící hodnota by se nedala
    # odlišit od „model si je jistý" a stránka by mlčky tvrdila víc, než ví.
    "required": ["relevantni", "nadpis", "shrnuti", "podstata_nalezena"],
}


# --------------------------------------------------------------------------
# Místopis
# --------------------------------------------------------------------------

def nacti_ulice() -> dict[str, str]:
    """Vrátí {porovnávací klíč: kanonický název ulice} z mistopis.json."""
    data = json.loads(MISTOPIS_FILE.read_text(encoding="utf-8"))
    return {klic_ulice(u): u for u in data["ulice"]}


# „ul. Havlíčkova" i „náměstí 5. května" mají vést na stejný klíč jako holý
# název ze slovníku. Předpona se proto strhává z obou stran porovnání.
RE_PREDPONA_ULICE = re.compile(r"^(?:ul\.|ulice|ulici|nám\.|náměstí)\s+", re.IGNORECASE)


def klic_ulice(s: str) -> str:
    """
    Porovnávací tvar názvu ulice: bez diakritiky, malými písmeny, bez
    interpunkce. „Obránců Míru" i „ul. Obránců míru" dají „obrancu miru".
    """
    s = RE_PREDPONA_ULICE.sub("", (s or "").strip())
    s = "".join(z for z in unicodedata.normalize("NFD", s) if not unicodedata.combining(z))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def rozeznej_ulice(navrzene: list, slovnik: dict[str, str]) -> tuple[list[str], list[str]]:
    """
    Rozdělí ulice od modelu na ty, které v Roztokách existují, a na zbytek.

    Neznámé se do zprávy nedostanou. Je to buď halucinace, nebo ulice v jiné
    obci, a obojí by ve zprávě vystupovalo jako ověřený fakt. Kdo potřebuje
    přesné vymezení, má o kliknutí dál originál. V dry runu se neznámé vypíšou,
    ať je poznat, jestli filtr nezahazuje něco správného.
    """
    ovrene, nezname = [], []
    for u in navrzene or []:
        kanonicka = slovnik.get(klic_ulice(u))
        if kanonicka and kanonicka not in ovrene:
            ovrene.append(kanonicka)
        elif not kanonicka and str(u).strip():
            nezname.append(str(u).strip())
    return ovrene, nezname


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


_parametry_vypsany = False


def vypis_prijate_parametry(root: ET.Element) -> None:
    """
    V dry runu jednou vypíše, které parametry server v odpovědi potvrdil.

    Slouží k ověření, jestli bere include_texts, nebo show_texts. Vypisují se
    schválně jen NÁZVY parametrů — hodnoty obsahují api_key a log běhu je ve
    veřejném repozitáři.
    """
    global _parametry_vypsany
    if _parametry_vypsany or not DRY_RUN:
        return
    _parametry_vypsany = True
    meta = root.find("./meta/requested_params")
    nazvy = sorted(set(re.findall(r'"(\w+)"\s*=>', meta.text or ""))) if meta is not None else []
    print(f"edesky potvrdilo parametry: {', '.join(nazvy) if nazvy else '(nevypsalo)'}")


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
                        # Dokumentace uvádí include_texts, ale server si
                        # v requested_params echuje show_texts. Posíláme oba —
                        # s jedním samotným chodily přílohy prázdné, přestože
                        # u nich edesky hlásilo contains_text='1'.
                        "include_texts": 1,
                        "show_texts": 1,
                        "order": "date",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! deska {dashboard_id}, '{kw}': {exc}", file=sys.stderr)
                continue

            vypis_prijate_parametry(root)

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
                    # Jen pro diagnostiku: kolik příloh dokument má a u kolika
                    # z nich edesky hlásí rozpoznaný text.
                    "prilohy": len(list(doc.iter("attachment"))),
                    "prilohy_s_textem": sum(
                        1 for a in doc.iter("attachment") if a.get("contains_text") == "1"
                    ),
                }
            time.sleep(1)  # slušnost vůči cizímu API

    return nalezene


def stahni_soupis_desky() -> list[dict]:
    """
    Holý soupis dokumentů na sledovaných deskách, bez ohledu na klíčová slova.

    Dotaz je `keywords=*`, protože parametr je povinný — bez něj edesky vrátí
    prázdno. Wildcard používá i oficiální ruby klient edesky jako výchozí
    hodnotu, takže se nespoléháme na nezdokumentované chování.

    Schválně bez `include_texts`/`show_texts`: tenhle průchod je jen pro
    stránku, aby na ní šlo dohledat i to, co hlídač neposlal. Texty by tekly
    po drátě zbytečně a do modelu z tohohle nejde nic.

    Selhání se nesmí dotknout hlavního běhu — soupis je vedlejší produkt.
    Když ho edesky nedá, vrátíme prázdno a hlídač pokračuje dál.
    """
    od = (date.today() - timedelta(days=DESKA_STAHOVAT_DNI)).isoformat()
    soupis = []

    for dashboard_id in DASHBOARDS:
        # Stránka má 200 dokumentů. Strop je pojistka proti nekonečné smyčce,
        # ne očekávaný stav: za týden se na desku tolik dokumentů nedostane.
        for stranka in range(1, SOUPIS_MAX_STRANEK + 1):
            try:
                root = edesky_get(
                    "documents",
                    {
                        "keywords": "*",
                        "dashboard_id": dashboard_id,
                        "created_from": od,
                        "order": "date",
                        "page": stranka,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! soupis desky {dashboard_id}: {exc}", file=sys.stderr)
                break

            na_strance = 0
            for doc in root.iter("document"):
                url = doc.get("edesky_url")
                if not url:
                    continue
                na_strance += 1
                soupis.append({
                    "url": url,
                    "nazev": doc.get("name", "").strip(),
                    "deska": doc.get("dashboard_name", ""),
                    "vlozeno": (doc.get("created_at", "") or "")[:10],
                })

            time.sleep(1)  # slušnost vůči cizímu API
            if na_strance < 200:
                break

    return soupis


def nacti_soupis() -> list:
    if DESKA_FILE.exists():
        return json.loads(DESKA_FILE.read_text(encoding="utf-8"))
    return []


def uloz_soupis(novy: list[dict]) -> int:
    """
    Přimíchá nově viděné dokumenty ke starým a ořeže soupis podle stáří.

    Vrací, kolik dokumentů přibylo. Starší než DESKA_DNI vypadnou — soupis
    má ukazovat souvislosti kolem toho, co hlídač poslal, ne suplovat archiv
    edesky.
    """
    stary = {z["url"]: z for z in nacti_soupis()}
    pribylo = sum(1 for z in novy if z["url"] not in stary)
    for z in novy:
        stary[z["url"]] = z

    mez = (date.today() - timedelta(days=DESKA_DRZET_DNI)).isoformat()
    vse = sorted(
        (z for z in stary.values() if z.get("vlozeno", "") >= mez),
        key=lambda z: (z.get("vlozeno", ""), z.get("nazev", "")),
        reverse=True,
    )

    if DRY_RUN:
        return pribylo
    DESKA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKA_FILE.write_text(
        json.dumps(vse, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return pribylo


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

def ma_text(dok: dict) -> bool:
    """Má dokument dost vlastního textu, aby šlo shrnutí opřít o obsah?"""
    return len(dok["text"].strip()) >= MIN_TEXT_CHARS


def stoji_za_upozorneni_bez_textu(dok: dict) -> bool:
    """
    Rozhodne o dokumentu, ke kterému nemáme text, jen podle názvu.

    Model se neptáme schválně: z holého názvu by relevanci hádal a hádání
    plodí falešné poplachy. Radši úzký deterministický filtr.
    """
    return bool(RE_SILNE.search(dok["nazev"]))


def holy_nalez(dok: dict) -> dict:
    """
    Náhrada za shrnutí u dokumentu bez rozpoznaného textu.

    Vědomě neobsahuje nic, co by se dalo splést se shrnutím obsahu — jen
    konstatování, že text není, a pobídku otevřít originál.
    """
    return {
        "relevantni": True,
        "nadpis": dok["nazev"],
        "shrnuti": (
            "edesky u tohoto dokumentu zatím nemá rozpoznaný text, takže shrnutí "
            "neexistuje a nechci si ho domýšlet. Otevřete prosím originál. "
            "Až text přibude, pošlu zprávu znovu i se shrnutím."
        ),
        # Bez textu není co najít, natož odkud opsat ulice.
        "podstata_nalezena": False,
        "ulice": [],
        "bez_textu": True,
    }


def prelozi_do_lidstiny(dok: dict) -> dict | None:
    """Pošle text do Gemini a vrátí strukturované shrnutí, nebo None."""
    text = (dok["nazev"] + "\n\n" + dok["text"])[:MAX_TEXT_CHARS]
    slovnik = nacti_ulice()

    vyveseno = dok.get("vlozeno", "")[:10] or "neuvedeno"
    zadani = PROMPT.format(
        text=text,
        vyveseno=vyveseno,
        ulice=", ".join(slovnik.values()),
    )
    payload = {
        "contents": [{"parts": [{"text": zadani}]}],
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
            shrnuti = json.loads(raw)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            print(f"  ! nečekaná odpověď Gemini: {exc}", file=sys.stderr)
            return None

        shrnuti["ulice"], shrnuti["ulice_nezname"] = rozeznej_ulice(
            shrnuti.get("ulice"), slovnik
        )
        return shrnuti

    print("  ! Gemini se nepodařilo zavolat ani na pátý pokus", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Výstup
# --------------------------------------------------------------------------

def nadpis(dok: dict, shrnuti: dict) -> str:
    return shrnuti.get("nadpis") or dok["nazev"]


# Den a měsíc ze začátku data, ať je z „04.09.2026 v 15:00" krátké „4. 9.".
RE_DEN_MESIC = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.")


def titulek(dok: dict, shrnuti: dict) -> str:
    """
    Nadpis issue, a tím i předmět e-mailu, který z něj GitHub rozešle.

    Předmět je jediné, co člověk uvidí v přehledu schránky, a ze samotného
    názvu vyhlášky se nepozná, jestli se to má stihnout tenhle týden, nebo
    za měsíc. Datum projednání proto jde dopředu. Když ho model nevrátil
    nebo mu nerozumíme, zůstane holý nadpis — vymýšlet si datum nebudeme.
    """
    t = nadpis(dok, shrnuti)
    m = RE_DEN_MESIC.search(shrnuti.get("datum_jednani") or "")
    return f"Projednání {int(m[1])}. {int(m[2])}. – {t}" if m else t


# Důraz řádku ve zprávě. NADPIS je vyhrazený datu veřejného projednání.
BEZNE, TUCNE, NADPIS = 0, 1, 2


def casti(shrnuti: dict) -> list[tuple[str, str, int]]:
    """
    Rozpadne zprávu na řádky (emoji, text, důraz).

    Datum veřejného projednání jde první a jako nadpis. Je to jediný údaj,
    který má podobu události — dá se na ni přijít a mluvit tam, a dá se
    zapsat do kalendáře. Lhůta pro připomínky je z něj odvozená a nastává
    až potom, takže stojí hned pod ním, ale slabší.

    Renderery níž z toho složí HTML pro Telegram nebo Markdown pro issue.
    Díky společnému základu se obě podoby nerozejdou, až se bude přidávat
    další pole.
    """
    r = []
    # Lokalita a ulice patří k sobě: obojí odpovídá na „týká se to mě?".
    # Lokalita je pro místní srozumitelnější, ulice přesnější, proto v tomhle
    # pořadí a na jednom řádku.
    misto = " · ".join(x for x in (
        shrnuti.get("lokalita", ""),
        ", ".join(shrnuti.get("ulice") or []),
    ) if x)
    if misto:
        r.append(("📍", misto, BEZNE))
    if shrnuti.get("datum_jednani"):
        r.append(("🗓", f"Veřejné projednání {shrnuti['datum_jednani']}", NADPIS))
        if shrnuti.get("misto_jednani"):
            r.append(("🏛", shrnuti["misto_jednani"], BEZNE))
    if shrnuti.get("deadline"):
        r.append(("⏳", f"Připomínky a námitky do: {shrnuti['deadline']}", TUCNE))
    if shrnuti.get("kdo_muze_podat"):
        r.append(("👤", shrnuti["kdo_muze_podat"], BEZNE))
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
    for emoji, text, duraz in casti(shrnuti):
        # Telegram nadpisy neumí, nejvýš tučné písmo — NADPIS tu splyne s TUCNE.
        t = esc_html(text)
        r.append(f"{emoji} " + (f"<b>{t}</b>" if duraz else t))
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
    for emoji, text, duraz in casti(shrnuti):
        t = esc_md(text)
        if duraz == NADPIS:
            # Prázdné řádky kolem: bez nich by nadpis splynul s odstavcem nad ním.
            r += ["", f"### {emoji} {t}", ""]
        else:
            r.append(f"{emoji} " + (f"**{t}**" if duraz else t))
    r.append("")
    r.append(esc_md(shrnuti.get("shrnuti", "")))
    r.append("")
    r.append(f"[dokument na edesky]({dok['url']})")
    if dok.get("orig_url"):
        r.append(f"[originál na úřední desce]({dok['orig_url']})")
    r.append("")
    r.append(f"*{esc_md(dok['deska'])}, vyvěšeno {esc_md(dok['vlozeno'][:10])}*")
    return "\n".join(r)


def diagnostika(dok: dict, shrnuti: dict) -> None:
    """
    Vypíše, co je v textu a co z toho model vytáhl. Jen pro dry run.

    Smysl: poznat rozdíl mezi „model to přehlédl" a „v dokumentu to není".
    Bez toho se prompt ladí naslepo. Vypisují se schválně jen nalezené vzory,
    ne text dokumentu — log běhu je ve veřejném repozitáři.
    """
    text = dok["text"]
    data = sorted(set(RE_DATUM.findall(text)))
    lhuty = sorted(set(m.strip() for m in RE_LHUTA.findall(text)))

    print(f"  rozbor: text {len(text)} znaků"
          f", příloh {dok.get('prilohy', '?')}"
          f", z toho s rozpoznaným textem {dok.get('prilohy_s_textem', '?')}"
          + (f", do modelu jde prvních {MAX_TEXT_CHARS}" if len(text) > MAX_TEXT_CHARS else ""))
    print(f"          data v textu: {', '.join(data) if data else '(žádné)'}")
    print(f"          lhůty v textu: {'; '.join(lhuty) if lhuty else '(žádné)'}")

    if not shrnuti:
        print("          modelu jsme se neptali (pod MIN_TEXT_CHARS)")
        return

    if not shrnuti.get("relevantni"):
        # U nerelevantního dokumentu jsou prázdná pole správně, ne chyba.
        print("          model označil dokument za nerelevantní")
        return

    print(f"          model vrátil: jednání={shrnuti.get('datum_jednani') or '—'!r}"
          f" lhůta={shrnuti.get('deadline') or '—'!r}")
    if data and not shrnuti.get("datum_jednani"):
        print("          ! v textu datum je, ale model žádné nevrátil")
    if lhuty and not shrnuti.get("deadline"):
        print("          ! v textu lhůta je, ale model ji nevrátil")

    if not shrnuti.get("podstata_nalezena"):
        print("          podstata v textu není, shrnutí to přiznává")
    ulice = shrnuti.get("ulice") or []
    print(f"          ulice ze slovníku: {', '.join(ulice) if ulice else '(žádné)'}")
    # Zahozené ulice jsou hlavní signál, že se prompt nebo slovník rozchází
    # s realitou. Bez tohohle výpisu by filtr tiše ubíral informace.
    if shrnuti.get("ulice_nezname"):
        print("          ! mimo slovník, zahozeno: "
              + ", ".join(shrnuti["ulice_nezname"]))


def posli(dok: dict, shrnuti: dict) -> list[str]:
    """
    Vypíše zprávu do logu a mimo dry run ji doručí.

    Vrací seznam kanálů, kterými se to povedlo — jde do archivu, ať je na
    stránce poznat, jestli zpráva opravdu někam dorazila.
    """
    telo = zprava_md(dok, shrnuti)
    print("\n" + "=" * 60)
    print(titulek(dok, shrnuti))
    print(telo)

    if DRY_RUN:
        print("\n  (dry run, neodesílám)")
        return []

    doruceno = []
    if posli_issue(titulek(dok, shrnuti), telo):
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


def nacti_bez_textu() -> set:
    if BEZ_TEXTU_FILE.exists():
        return set(json.loads(BEZ_TEXTU_FILE.read_text(encoding="utf-8")))
    return set()


def uloz_bez_textu(cekajici: set) -> None:
    if DRY_RUN:
        return
    BEZ_TEXTU_FILE.parent.mkdir(parents=True, exist_ok=True)
    BEZ_TEXTU_FILE.write_text(
        json.dumps(sorted(cekajici)[-500:], ensure_ascii=False, indent=1),
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
        "ulice": shrnuti.get("ulice") or [],
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
        # Schválně bez bool(): None znamená „nebylo se koho ptát" a stránka
        # ho musí umět odlišit od tvrzení „podstata chybí".
        "podstata_nalezena": shrnuti.get("podstata_nalezena"),
        "bez_textu": bool(shrnuti.get("bez_textu")),
    }


# --------------------------------------------------------------------------

def spust_nahled(dokumenty: dict) -> None:
    """
    Přeloží nalezené dokumenty a uloží výsledek do state/nahled.json.

    Proti ostrému běhu schválně jinak: nekouká na state/seen.json, takže projde
    i dokumenty, na které se už upozornilo, a zapíše i ty, které model označil
    za nerelevantní. Smysl je posoudit kvalitu překladu a chování filtru na
    víc než jednom vzorku — proto je potřeba vidět i to, co propadlo.
    """
    vzorek = list(dokumenty.values())[:NAHLED_MAX]
    print(f"Náhled: beru {len(vzorek)} z {len(dokumenty)} dokumentů"
          + (f" (strop NAHLED_MAX={NAHLED_MAX})" if len(dokumenty) > NAHLED_MAX else ""))

    zaznamy = []
    for i, dok in enumerate(vzorek, 1):
        print(f"\n[{i}/{len(vzorek)}] → {dok['nazev'][:80]}")

        if not ma_text(dok):
            print("  (bez rozpoznaného textu, modelu se neptám)")
            z = zaznam(dok, holy_nalez(dok), [])
            z["relevantni"] = None       # None = nebylo co posuzovat
            zaznamy.append(z)
            continue

        shrnuti = prelozi_do_lidstiny(dok)
        if shrnuti is None:
            print("  (nepodařilo se zpracovat)")
            continue

        diagnostika(dok, shrnuti)
        z = zaznam(dok, shrnuti, [])
        z["relevantni"] = bool(shrnuti.get("relevantni"))
        zaznamy.append(z)
        print("  " + ("PROŠLO filtrem" if z["relevantni"] else "model zahodil"))
        time.sleep(4)   # ohled na limity free tieru

    NAHLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    NAHLED_FILE.write_text(
        json.dumps(zaznamy, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    proslo = sum(1 for z in zaznamy if z.get("relevantni"))
    print(f"\nNáhled hotov: {len(zaznamy)} záznamů, z toho {proslo} prošlo filtrem.")
    print(f"Zapsáno do {NAHLED_FILE}.")


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--najdi-desku":
        najdi_desku(sys.argv[2])
        return 0

    for jmeno in ("EDESKY_API_KEY", "GEMINI_API_KEY"):
        if not os.environ.get(jmeno):
            print(f"Chybí proměnná {jmeno}", file=sys.stderr)
            return 2

    if NAHLED:
        print(f"Náhledový režim: {LOOKBACK_DAYS} dní zpět, desky "
              f"{', '.join(DASHBOARDS)}. Nic se neodešle a ostrý stav zůstane.")
        spust_nahled(stahni_dokumenty())
        return 0

    videne = nacti_stav()
    bez_textu = nacti_bez_textu()
    archiv = nacti_archiv()
    print(f"Znám {len(videne)} dokumentů, dívám se {LOOKBACK_DAYS} dní zpět "
          f"na desky {', '.join(DASHBOARDS)}.")
    if bez_textu:
        print(f"{len(bez_textu)} dokumentů čeká, až u nich edesky rozpozná text.")
    if DRY_RUN:
        print("Dry run: stav ani archiv se neuloží a nic se neodešle.")

    dokumenty = stahni_dokumenty()
    nove = {u: d for u, d in dokumenty.items() if u not in videne}
    print(f"Nalezeno {len(dokumenty)} dokumentů, z toho {len(nove)} nových.")

    poslano = 0
    for url, dok in nove.items():
        print(f"\n→ {dok['nazev'][:80]}")

        if not ma_text(dok):
            # Bez textu se neptáme modelu — vymyslel by shrnutí z názvu.
            if DRY_RUN:
                diagnostika(dok, {})
            if url in bez_textu:
                print("  (pořád bez textu, na holý nález jsem už upozornil)")
                continue
            if not stoji_za_upozorneni_bez_textu(dok):
                print("  (bez textu a název nenapovídá územnímu plánování, "
                      "přeskakuji)")
                continue
            print("  ! bez rozpoznaného textu, posílám holý nález")
            shrnuti = holy_nalez(dok)
            doruceno = posli(dok, shrnuti)
            archiv.insert(0, zaznam(dok, shrnuti, doruceno))
            bez_textu.add(url)   # do videne schválně ne, ať se vrátíme s textem
            poslano += 1
            time.sleep(5)
            continue

        shrnuti = prelozi_do_lidstiny(dok)
        videne.add(url)

        if shrnuti is None:
            print("  (nepodařilo se zpracovat, příště se na něj podívám znovu)")
            videne.discard(url)
            continue

        if DRY_RUN:
            diagnostika(dok, shrnuti)

        if not shrnuti.get("relevantni"):
            print("  (netýká se územního plánování, přeskakuji)")
            bez_textu.discard(url)
            continue

        # Text dorazil až teď, takže tenhle dokument už nečeká na OCR.
        bez_textu.discard(url)

        doruceno = posli(dok, shrnuti)
        # Nejnovější nahoru, stránka to tak čte bez dalšího řazení.
        archiv.insert(0, zaznam(dok, shrnuti, doruceno))
        poslano += 1
        time.sleep(5)  # ať se nepereme s limitem free tieru

    uloz_stav(videne)
    uloz_bez_textu(bez_textu)
    uloz_archiv(archiv)

    # Až po odeslání: soupis je jen pro stránku a nesmí zdržet doručení.
    soupis = stahni_soupis_desky()
    if soupis:
        pribylo = uloz_soupis(soupis)
        print(f"Soupis desky: {len(soupis)} dokumentů za období, "
              f"{pribylo} z nich nových.")
    else:
        print("Soupis desky se nepodařilo načíst, stránka zůstane u starého.")

    print(f"\nHotovo. Zpráv k odeslání: {poslano}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
