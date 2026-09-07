# CLAUDE.md

Kontext pro Claude Code pracujícího v tomhle repozitáři.

## Co to je

Hlídač úředních desek pro město Roztoky u Prahy. Jednou denně stáhne nové
dokumenty z úředních desek, vytáhne ty o územním plánování, nechá je jazykovým
modelem přeložit do srozumitelné češtiny a doručí je jako GitHub issue
(e-mail z něj rozešle GitHub sám) a volitelně na Telegram. Archiv zpráv drží
`state/zpravy.json` a vykresluje ho statická stránka `index.html`.

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

## Architektura

Jeden soubor, `hlidac.py`, bez frameworků. K tomu jedna statická stránka,
`index.html`, bez buildu a bez frameworku. Je to nástroj, který má běžet roky
bez údržby, ne aplikace, která má růst. Držme to tak.

```
GitHub Actions (cron)
  → edesky API v1 (XML) pro každou desku × každé klíčové slovo
  → dedup podle edesky_url proti state/seen.json
  → Gemini generateContent se strukturovaným výstupem (responseSchema)
  → filtr podle pole "relevantni"
  → GitHub issue (e-mail rozešle GitHub) + volitelně Telegram sendMessage
  → zápis do state/zpravy.json
  → commit state/ zpátky do repa

GitHub Pages (větev main, složka /)
  → index.html si fetchne state/zpravy.json a vykreslí archiv
```

Externí závislosti: jen `requests`. Stránka nemá žádné — jen dva fonty
z Google Fonts. Nepřidávat další bez dobrého důvodu.

**Doručuje se přes issues, ne přes SMTP.** Vestavěný `GITHUB_TOKEN` stačí,
takže není co konfigurovat a není app password, který by za rok vypršel.
Bonus: nálezy mají trvalou stopu a dají se odklikávat jako vyřízené.

**Tvar záznamu v `state/zpravy.json` je veřejné API stránky.** Funkce
`zaznam()` a čtení v `index.html` se musí měnit spolu.

## Zásady, které nerušit bez zeptání

**Texty dokumentů se berou z `include_texts=1`, ne z `edesky_text_url`.**
edesky je za bot ochranou Anubis, druhé HTTP volání na stažení textu je zbytečné
riziko. Když API vrátí místo XML něco jiného, `edesky_get()` to pozná a vyhodí
srozumitelnou chybu — tuhle kontrolu neodstraňovat.

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

**Relevanci posuzuje model, ne jen klíčová slova.** Fulltext vytáhne pod
„opatření obecné povahy" i dopravní uzavírky, kterých je na desce spousta.
Filtr přes pole `relevantni` je to, co drží počet notifikací nízko.

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
```

`DRY_RUN=1` neukládá stav a neposílá — používej ho při každé změně filtrů nebo
promptu. Ověřovat proti reálným datům z posledních 30 dní je mnohem užitečnější
než psát unit testy na parsování XML.

Při úpravě `PROMPT` nebo `KEYWORDS` vždy nejdřív dry run a projít výstup ručně.
Zajímá nás, co filtrem propadlo, ne jen co prošlo.

## Jazyk

Kód, komentáře, commit messages i výstup pro uživatele česky. Prompt pro model
česky. Uživatelé jsou obyvatelé Roztok, ne vývojáři.
