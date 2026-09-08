# CLAUDE.md

Kontext pro Claude Code pracujícího v tomhle repozitáři.

## Co to je

Hlídač úředních desek pro město Roztoky u Prahy. Jednou denně stáhne nové
dokumenty z úředních desek, vytáhne ty o územním plánování, nechá je jazykovým
modelem přeložit do srozumitelné češtiny a doručí je jako GitHub issue
(e-mail z něj rozešle GitHub sám) a volitelně na Telegram. Archiv zpráv drží
`state/zpravy.json` a vykresluje ho statická stránka `index.html`: nahoře
kalendář nadcházejících termínů, pak nálezy, dole sbalený soupis toho, co
hlídač neposlal.

**Proč to vzniklo:** v Roztokách proběhlo veřejné projednání regulačního plánu
lokality Panenská II a téměř nikdo o něm nevěděl. Oznámení viselo na úřední
desce pod názvem typu „VV - územní opatření o stavební uzávěře - Panenská II,
úde. č. 182", ze kterého běžný člověk nepozná, že se ho to týká. Aplikace
V obraze posílá všechno včetně odstávek elektřiny, takže si ji lidé ztlumí.

Cílem tedy není jen doručit notifikaci, ale doručit **málo** notifikací, které
dávají smysl bez právního vzdělání. Falešně pozitivní zpráva je horší než delší
kód — protože po třetí zbytečné notifikaci si uživatel hlídač vypne a mine
i tu jednu důležitou.

## Věcný kontext, který není zřejmý z kódu

- Pořizovatelem územního plánu i regulačních plánů Roztok je **odbor územního
  plánování MěÚ Černošice** (obec s rozšířenou působností). Veřejné vyhlášky
  proto visí na dvou deskách současně — roztocké i černošické. Sledovat je
  potřeba obě, proto je `EDESKY_DASHBOARDS` seznam. Roztoky mají ID 260.
- **Národní geoportál územního plánování jako zdroj nepoužívat.** Povinné
  používání se opakovaně odkládá a geoportál plní zatím jen orientační funkci.
  Právně závazné je vyvěšení na úřední desce.
- Roztoky nejsou ORP, takže na ně nedopadá povinnost publikovat úřední desku
  jako otevřená data. Proto edesky.cz a ne otevřená data. Černošice tuhle
  povinnost mají, takže pro ně existuje i cesta přes otevřenou formální normu
  (<https://ofn.gov.cz/úřední-desky/>) — použitelné jako záloha, kdyby edesky
  vypadlo.
- Lhůty pro námitky běží od data vyvěšení, ne od načtení na edesky. Rozdíl bývá
  den, ale u posledního dne lhůty na tom záleží.
- **`keywords` je u edesky API povinný parametr.** Bez něj server nevrátí
  chybu, ale prázdný výsledek — což se ladí mizerně. Na výpis celé desky se
  posílá `keywords=*`, jak to dělá i oficiální ruby klient edesky. Stránkuje
  se po 200 dokumentech parametrem `page`.
- Roztocká deska má řádově **45 dokumentů za měsíc**, z nichž se územního
  plánování týkají jednotky. Objem je malý, takže se soupis desky vejde do
  repozitáře i do stránky bez stránkování a lazy loadingu.
- **Co na desce doopravdy visí** (ručně roztříděných 113 dokumentů z března až
  září 2026): územní plánování 4, stavební záměry na konkrétní RD 7, doprava 5,
  přerušení dodávky elektřiny 4, životní prostředí 4, samospráva zhruba 37
  (veřejnoprávní smlouvy s dotovanými spolky, pozvánky na zastupitelstvo,
  závěrečné účty, záměry pronájmu). Zbytek, tedy skoro polovinu, tvoří volby.
  Z toho plyne několik věcí, které jdou proti intuici:
  - **Odstávky elektřiny na desce jsou.** Argument z README, že aplikace
    V obraze zaplavuje lidi odstávkami, tedy nelze vyřešit jinou datovou
    trubkou. Řeší ho jen filtr.
  - **Volby desku o volebním roce zahltí.** 30 z těch dokumentů jsou
    rozhodnutí o registraci kandidátních listin, a většina pro *jiné obce* —
    Holubice, Statenice, Únětice, Libčice, Horoměřice. Roztoky je vyvěšují
    proto, že je rozesílá ORP. Kdyby se záběr hlídače rozšiřoval, filtr podle
    územní působnosti je potřebnější než další klíčová slova.
  - **Za půl roku ani jedno oznámení o uložení písemnosti nebo dražba.** Obava
    o osobní údaje v textech je tedy menší, než by se čekalo, ale nezmizela:
    jména a parcely jsou ve stavebních záměrech a ve vyhláškách o dani
    z nemovitých věcí.
- **GitHub nemá atom feed pro issues** (`/issues.atom` vrací 406), jen pro
  commity. Kdyby měl někdo chtít odběr jinam než přes e-mail z issue, feed
  by si musel hlídač generovat sám ze `zpravy.json`. Nejlevnější cesta, jak
  rozeslat nálezy dalším lidem bez psaní kódu, je nechat repozitář sledovat
  účet, jehož e-mail je adresa skupiny nebo konference.

## Architektura

Jeden soubor, `hlidac.py`, bez frameworků. K tomu jedna statická stránka,
`index.html`, bez buildu a bez frameworku. Je to nástroj, který má běžet roky
bez údržby, ne aplikace, která má růst. Držme to tak.

```
GitHub Actions (cron)
  → edesky API v1 (XML) pro každou desku × každé klíčové slovo
  → dedup podle edesky_url proti state/seen.json
  → Gemini generateContent se strukturovaným výstupem (responseSchema),
    v promptu jede i seznam ulic z mistopis.json
  → ulice z odpovědi se proti témuž seznamu ověří, neznámé se zahodí
  → filtr podle pole "relevantni"
  → GitHub issue (e-mail rozešle GitHub) + volitelně Telegram sendMessage
  → zápis do state/zpravy.json
  → po odeslání ještě holý soupis desky (keywords=*, bez textů)
    → state/deska.json
  → commit state/ zpátky do repa

GitHub Pages (větev main, složka /)
  → index.html si fetchne state/zpravy.json a vykreslí archiv
  → a state/deska.json, ze kterého dole vypíše, co neprošlo
```

Externí závislosti: jen `requests`. Stránka nemá žádné — jen dva fonty
z Google Fonts. Nepřidávat další bez dobrého důvodu.

**Doručuje se přes issues, ne přes SMTP.** Vestavěný `GITHUB_TOKEN` stačí,
takže není co konfigurovat a není app password, který by za rok vypršel.
Bonus: nálezy mají trvalou stopu a dají se odklikávat jako vyřízené.

**Tvar záznamu v `state/zpravy.json` je veřejné API stránky.** Funkce
`zaznam()` a čtení v `index.html` se musí měnit spolu.

**Datum veřejného projednání má přednost před lhůtou.** Ve zprávě je nadpis
(`NADPIS` v `casti()`) a je i v titulku issue, tedy v předmětu e-mailu, který
z něj GitHub rozešle. Na stránce má větší písmo a plný štítek s odpočtem,
zatímco lhůta jen obrys. Důvod: projednání je jediný údaj, který má podobu
události — dá se na ni přijít a mluvit tam. Lhůta pro připomínky je z něj
odvozená a nastává až po něm. Když model datum nevrátí, zůstane titulek holý;
dopočítávat ho odjinud nebudeme.

## Zásady, které nerušit bez zeptání

**Texty dokumentů se berou z parametrů u dotazu, ne z `edesky_text_url`.**
Posílají se `include_texts=1` i `show_texts=1`. Dokumentace uvádí první,
server si v `requested_params` echuje druhý — a se samotným `include_texts`
chodily přílohy prázdné, přestože u nich edesky hlásilo `contains_text='1'`.
Ani jeden z nich neodstraňovat.

Text se nestahuje zvlášť z `edesky_text_url`, protože edesky je za bot ochranou
Anubis a druhé HTTP volání je zbytečné riziko. Když API vrátí místo XML něco
jiného, `edesky_get()` to pozná a vyhodí srozumitelnou chybu — tuhle kontrolu
neodstraňovat.

**`MAX_TEXT_CHARS` je nastaveno nízko schválně.** Free tier Gemini si smí
odeslaný obsah použít ke zlepšování svých produktů. Vyhlášky obsahují v části
„doručí se veřejnou vyhláškou" jména a adresy konkrétních lidí, a ty jsou až na
konci dokumentu. Ořez na prvních 6000 znaků je ochrana osobních údajů třetích
osob, ne úspora tokenů. Kdyby bylo potřeba posílat víc, je správná odpověď
zapnout placený tier, ne zvednout limit.

**Když se dokument nepodaří zpracovat, nezapisuje se mezi viděné.** Ať se na něj
příště podívá znovu. Radši dvě zprávy než žádná.

**Bez textu se model neptáme na shrnutí.** edesky u části dokumentů nemá
rozpoznaný text — naskenované PDF bez OCR, nebo ho ještě nestihlo zpracovat.
Dřív se v takovém případě do modelu poslal samotný název a model si shrnutí
vymyslel; u vyhlášky o RP Solníky vznikly věty o „stavebních a prostorových
regulativech", které v žádném dokumentu nebyly. Proto `MIN_TEXT_CHARS`: pod tím
se posílá holý nález s odkazem a s přiznáním, že shrnutí není. Vymyšlené
shrnutí je horší než žádné — celý smysl nástroje stojí na tom, že se tomu, co
pošle, dá věřit.

**Dokument bez textu se drží v `state/bez_textu.json`, ne v `seen.json`.**
Upozorní se na něj jednou holým nálezem, ale zůstane ve frontě, takže až u něj
edesky text rozpozná, projde modelem a přijde znovu i se shrnutím. Kdyby se
zapsal mezi viděné, propásli bychom obsah natrvalo.

**O relevanci dokumentu bez textu rozhoduje `RE_SILNE`, ne model.** Z holého
názvu by model relevanci hádal. Regex je užší než `KEYWORDS` schválně —
nejsou v něm „opatření obecné povahy" ani názvy lokalit. Pozor na skloňování:
„o stavební uzávěř**e**" je s ř, ne s r.

**Model musí umět říct „nevím" — od toho je `podstata_nalezena`.** Vyhlášky
o územním plánování často neuvádějí, co se v území mění; odkážou na výkresy
nebo na web města. Dokud pipeline nechodí za odkazem, model nemá odkud vzít
obsah a dřív ho zaléval obecnou větou („dokumentace mění dosavadní podmínky
v území"). Když je pole false, shrnutí to přizná první větou a stránka přidá
štítek. **V archivu se drží i `null`** pro záznamy z doby před tímhle polem —
`false` je tvrzení, chybějící hodnota není, a stránka to musí odlišit
(`podstata_nalezena === false`, ne `!podstata_nalezena`).

**Ulice se ověřují proti `mistopis.json`, co není ve slovníku, jde pryč.**
Seznam je z RÚIAN (ČÚZK, kód obce Roztoky je **539627** — pozor, 539660 jsou
Slapy), obnovuje se stažením, adresa je v souboru. Posílá se i modelu, aby
ulice psal kanonicky. Ulice mimo slovník se do zprávy nedostane: je to buď
halucinace, nebo ulice v jiné obci, a ve zprávě by obojí vypadalo jako
ověřený fakt. V dry runu se zahozené vypisují — bez toho by filtr tiše ubíral
informace. Porovnává se bez diakritiky a bez předpony „ul." / „nám.", takže
„ul. Obránců Míru" sedne na „Obránců míru".

Žalov ve slovníku je: v RÚIAN není samostatnou částí obce, je to katastrální
území, a jeho ulice jsou v seznamu Roztok. Tabulka *lokalita → ulice*
(Solníky → které ulice) zatím **neexistuje** a nedá se odvodit z dat — chce to
místní znalost, ne generování.

**Nadpis a shrnutí mají v promptu vlastní pravidla, včetně zakázaných vět.**
Nadpis říká, co se děje a kde, ne jaký je to typ dokumentu. Shrnutí odpovídá
v pořadí: co se mění → koho se to týká → co s tím můžu dělat a do kdy. Seznam
zakázaných frází v promptu není ozdoba: jsou to věty, které model psal a které
sedí na každou vyhlášku, takže nenesou žádnou informaci. Když se přidává nová,
patří tam celá, ne jen její téma.

**Pomlčka je „–", nikdy „—".** Platí pro prompt, pro výstup modelu i pro
texty stránky a titulek issue.

**Relevanci posuzuje model, ne jen klíčová slova.** Fulltext vytáhne pod
„opatření obecné povahy" i dopravní uzavírky, kterých je na desce spousta.
Filtr přes pole `relevantni` je to, co drží počet notifikací nízko.

**Soupis desky (`state/deska.json`) je jen pro stránku a nesmí do modelu.**
Stahuje se až po odeslání, dotazem `keywords=*` — parametr je u edesky
povinný a wildcard používá i jejich oficiální ruby klient. Schválně bez
`include_texty`/`show_texts`: z tohohle průchodu nejde do Gemini nic a ani
se nestahují texty, ve kterých bývají jména účastníků řízení. Soupis dává
stránce možnost ukázat, co hlídač neposlal — bez něj nejde poznat rozdíl
mezi „na desce nic nebylo" a „filtr to zahodil". Selhání soupisu nesmí
shodit běh; je to vedlejší produkt, ne doručení.

**U dokumentů ze soupisu se nedopisuje shrnutí.** Vypisují se holé: datum,
název, odkaz. Model je nikdy neviděl (neprošly klíčovými slovy) nebo je
zahodil — vymyslet k nim popis by bylo přesně to, čemu se hlídač jinde
vyhýbá.

**Heartbeat commit se nesmí zrušit.** GitHub vypíná scheduled workflows po
60 dnech bez aktivity v repozitáři. Commit stavu nastane jen když se něco najde,
takže při klidnějším období by se hlídač tiše vypnul — přesně před tím, na co
existuje. Krok „Uložit stav" proto po 30 dnech bez commitu udělá prázdný commit.
Práh je půlka lhůty schválně: jeden vynechaný nebo spadlý běh nesmí stačit
k tomu, aby se propáslo okno.

**Dry run nesmí nic odeslat.** `DRY_RUN=1` znamená neuložit stav, nezapsat do
archivu a nezaložit issue. Kontrola je v `posli()` hned na začátku — kdyby se
odesílání větvilo jinam, musí ji dostat i nová větev.

## Práce se secrets

**Nikdy nenastavuj secrets a nežádej o hodnoty API klíčů.** Když je potřeba
něco doplnit, napiš uživateli příkaz `gh secret set NÁZEV` a nech ho, ať to
udělá sám — `gh` se na hodnotu zeptá interaktivně a klíč se nedostane do
kontextu ani do historie shellu.

Klíče se čtou výhradně z proměnných prostředí. Žádné defaulty, žádné fallbacky
na soubor, žádné logování hodnot.

## Testování

```bash
python -c "import ast; ast.parse(open('hlidac.py').read())"          # syntax
EDESKY_API_KEY=... python hlidac.py --najdi-desku Černošice          # živý test API
gh workflow run "Hlídač úředních desek" -f lookback_days=30 -f dry_run=1
gh workflow run "Hlídač úředních desek" -f nahled=1 -f lookback_days=45
```

Stránku je nejrychlejší zkusit lokálním serverem (`python3 -m http.server`)
proti skutečnému `state/`; přes `file://` selže fetch na CORS.

`DRY_RUN=1` neukládá stav a neposílá — používej ho při každé změně filtrů nebo
promptu. Ověřovat proti reálným datům z posledních 30 dní je mnohem užitečnější
než psát unit testy na parsování XML.

Při úpravě `PROMPT` nebo `KEYWORDS` vždy nejdřív dry run a projít výstup ručně.
Zajímá nás, co filtrem propadlo, ne jen co prošlo.

## Jazyk

Kód, komentáře, commit messages i výstup pro uživatele česky. Prompt pro model
česky. Uživatelé jsou obyvatelé Roztok, ne vývojáři.
