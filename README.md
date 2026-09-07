# Hlídač veřejných projednání územního plánu

Sleduje úřední desky, vytáhne z nich dokumenty o územním plánování, nechá je
přeložit do srozumitelné češtiny a doručí je.

Nálezy chodí **jako issue v tomhle repozitáři** — e-mail o nich rozešle GitHub
sám všem, kdo repozitář sledují. Není co konfigurovat a není heslo, které by za
rok vypršelo. Telegram jde zapnout navíc.

Archiv všeho, co hlídač našel, je na stránce
<https://elipsoid-cz.github.io/roztocka-ptydeska/>.

## Struktura repozitáře

```
hlidac.py                     celý hlídač
index.html                    stránka s archivem zpráv, čte state/zpravy.json
.github/workflows/hlidac.yml  denní spouštění
state/                        vznikne samo po prvním ostrém běhu
  seen.json                     už viděné dokumenty (dedup)
  zpravy.json                   archiv zpráv, ze kterého čte stránka
```

## Nastavení

### 1. Klíč k edesky

Registrace na <https://edesky.cz>, klíč pak najdete v uživatelském profilu.
Používání API vyžaduje registraci a běží zatím v testovacím provozu — počítejte
s tím, že to není služba se zárukou dostupnosti.

### 2. Klíč ke Gemini

Na <https://aistudio.google.com/api-keys> přes Google účet, bez platební karty.
Projekt zůstane na Free tieru.

Než to spustíte, mrkněte na <https://aistudio.google.com/rate-limit>, jaké
limity váš projekt aktuálně má — Google je v dokumentaci už neuvádí a mohou se
měnit. Skript posílá jednotky požadavků denně, takže se vejdete, ale je dobré
vědět, kde to zkontrolovat.

**Free tier znamená, že Google může odeslaný obsah použít ke zlepšování svých
produktů.** Skript proto posílá jen prvních 6000 znaků dokumentu (podstatné je
na první stránce, rozdělovník se jmény a adresami až na konci). Když vám to
nestačí, zapněte v AI Studiu fakturaci — na Paid tieru se obsah k trénování
nepoužívá a při tomhle objemu zaplatíte jednotky korun ročně.

### 3. Doručování

**E-mailem přes issues.** Nic nenastavujete. Workflow má `issues: write`
a zakládá issue vestavěným `GITHUB_TOKEN`. Aby vám o nich chodil e-mail,
mějte repozitář ve Watch → All Activity (u vlastního repa to bývá zapnuté)
a v <https://github.com/settings/notifications> zapnutý e-mail.

**Telegram, volitelně.** Napište [@BotFather](https://t.me/BotFather),
`/newbot`, dostanete token. Pak si s botem napište (nebo ho přidejte do
skupiny) a chat ID zjistíte na
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

Bez obojího se zprávy jen vypíšou do logu běhu.

### 4. Secrets a proměnné v repozitáři

Settings → Secrets and variables → Actions. Nebo z příkazové řádky — `gh` se
na hodnotu zeptá interaktivně, takže klíč neskončí v historii shellu:

```bash
gh secret set EDESKY_API_KEY
gh secret set GEMINI_API_KEY
```

Volitelně `TELEGRAM_BOT_TOKEN` a `TELEGRAM_CHAT_ID`.
`GITHUB_TOKEN` nenastavujte, ten si Actions doplní samy.

Variables: `EDESKY_DASHBOARDS` — čárkou oddělená ID desek.
Roztoky mají ID **260** (<https://edesky.cz/desky/260>).

### 5. Stránka s archivem

Settings → Pages → Source: Deploy from a branch, větev `main`, složka `/`.
Stránka čte `state/zpravy.json`, takže se aktualizuje sama po každém běhu,
při kterém se něco našlo. Do prvního nálezu ukazuje „Zatím ticho".

### 6. Přidat Černošice

Pořizovatelem územního plánu i regulačních plánů Roztok je odbor územního
plánování MěÚ Černošice, takže veřejné vyhlášky visí i na jejich desce. Její ID
zjistíte takto:

```bash
EDESKY_API_KEY=... python hlidac.py --najdi-desku Černošice
```

Pak do `EDESKY_DASHBOARDS` dejte `260,<id Černošic>`.

## První spuštění

Actions → Hlídač úředních desek → Run workflow. Nastavte
`lookback_days` na 30 a `dry_run` na 1 — uvidíte, co by to poslalo, aniž by se
uložil stav. Až budete spokojený s tím, co prochází filtrem, pusťte to naostro.

## Ladění

- **Chodí toho moc** → zužte `KEYWORDS` v `hlidac.py`. Největší šum dělá
  „opatření obecné povahy", pod které spadají i dopravní uzavírky.
- **Něco propadlo** → přidejte lokalitu do `KEYWORDS` a zvyšte `MAX_TEXT_CHARS`.
- **Model špatně soudí relevanci** → upravte `PROMPT`, hlavně výčet toho, co
  relevantní *není*.
- **edesky vrací místo XML „Access Denied"** → web je za bot ochranou Anubis.
  Skript to pozná a napíše to do logu. Pokud se to bude dít pravidelně, napište
  jim; jsou to lidé z občanského projektu, ne korporát.

## Co tenhle hlídač nezaručuje

Není to náhrada za sledování úřední desky, je to pojistka. edesky načítá desky
s nějakým zpožděním, `created_from` filtruje podle data načtení na edesky a ne
podle data vyvěšení, a lhůty pro námitky běží od vyvěšení. Nechte si proto
zapnutý i e-mailový hlásič přímo na edesky.cz jako druhou vrstvu.

Národní geoportál územního plánování jako zdroj nepoužívejte — povinné
používání se opakovaně odkládá a zatím plní jen orientační funkci. Závazné je
vyvěšení na úřední desce.
