# University AI Knowledge Hub — Telegram бот + сайт

Бір білім қоры, бір промпт, екі интерфейс:

```
core.py            ← бүкіл логика: knowledge/ оқу, Gemini, қателер, тілдер
├── telegram_bot.py  Telegram боты (handler-лер)
│   └── bot.py       polling режимінде іске қосу  →  python bot.py
└── main.py          сайт + REST API (+ webhook)  →  uvicorn main:app --reload
```

Бот пен сайт бір `core.py`-ды қолданады, сондықтан екеуі әрқашан **бірдей
құжаттардан бірдей жауап** береді. Жаңа мүмкіндікті бір жерге қоссаң,
екеуінде де жұмыс істейді.

---

## 1. Орнату

```bash
pip install -r requirements.txt
cp env.example .env        # Windows: copy env.example .env
```

`.env` файлын ашып толтыр:

| Айнымалы | Қайдан аласың |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `GEMINI_MODEL` | мысалы `gemini-flash-latest` |
| `TELEGRAM_BOT_TOKEN` | Telegram-дағы **@BotFather** → `/newbot` |

> ⚠️ Бұрынғы кілтің чатқа жүктелген `.env` файлында ашық тұрды — AI Studio-да
> оны өшіріп, жаңасын жасап ал.

Жобаны git-ке қоссаң, түбірде `.gitignore` файлын жаса:

```gitignore
.env
bot_state.pickle
__pycache__/
*.pyc
```

`.env` бір рет коммитке түссе, кейін өшірсең де git тарихында қалады —
сондықтан алдымен `.gitignore`.

## 1.5. Тексеру

```bash
python selftest.py
```

Кітапханалар, `.env`, `knowledge/`, нұсқаулық, Gemini жауабының уақыты және
бот токені — бәрі бір команда мен тексеріледі. Демо алдында осыны қос.

## 2. Білім қорын толтыру

`knowledge/` папкасына университеттің бекітілген құжаттарын сал (`.md`,
`.txt`, `.docx`). Толық нұсқаулық — `knowledge/README.md`.

Файлдар өзгергенде автоматты түрде қайта оқылады — серверді қайта қосудың
қажеті жоқ.

## 3. Ботты іске қосу

```bash
python bot.py
```

Терминалда `connected as @sening_botyn` деп шықса — дайын. Telegram-нан ботты
тауып `/start` бас.

**Командалар**

| Команда | Не істейді | User story |
|---|---|---|
| — (жай мәтін) | Сұраққа білім қорынан жауап береді | US5 |
| `/guide` | Расписание құру нұсқаулығы + портал батырмасы | US3 |
| `/lang` | Қазақша / Русский / English | US6 |
| `/reset` | Әңгіме тарихын тазартады | — |
| `/help` | Анықтама | — |

Тіл мен әңгіме тарихы әр қолданушыға бөлек сақталады (`bot_state.pickle`),
сондықтан ботты қайта қосқанда да жоғалмайды.

## 4. Сайтты іске қосу

```bash
uvicorn main:app --reload
```

→ http://127.0.0.1:8000

| Endpoint | Не қайтарады |
|---|---|
| `GET /` | `static/index.html` |
| `POST /api/chat` | `{message, history, lang}` → `{reply}` |
| `GET /api/guide?lang=kk` | нұсқаулық мәтіні |
| `GET /api/health` | сервер тірі ме, қандай құжаттар көрінеді |

`/api/health` — демо алдында тексеруге ыңғайлы: `documents` тізімі бос болса,
`knowledge/` папкасы бос деген сөз.

Бот пен сайтты **бір уақытта** қосуға болады — екі бөлек терминал:

```bash
python bot.py                    # 1-терминал
uvicorn main:app --reload        # 2-терминал
```

## 5. Webhook режимі (серверге шыққанда)

Ноутбукте polling жеткілікті. Хостингке (Railway, Render, VPS) қойғанда бір
ғана процесс сайтты да, ботты да ұстай алады:

```ini
TELEGRAM_MODE=webhook
TELEGRAM_WEBHOOK_URL=https://sening-domenin.up.railway.app
TELEGRAM_WEBHOOK_SECRET=кездейсоқ_ұзын_жол
```

Сосын тек `uvicorn main:app --host 0.0.0.0 --port $PORT` қосасың — `bot.py`
керек емес. Webhook-ты Telegram-ға тіркеу автоматты түрде жүреді.

`TELEGRAM_WEBHOOK_SECRET` — Telegram әр сұраныста қайтаратын құпия жол;
онсыз webhook-қа кез келген адам жалған хабар жібере алады.

## 5.1. Vercel-ге деплой

Vercel — serverless: тұрақты процесс жоқ, әр сұраныс жеке, қысқа өмір
сүретін функцияда өңделеді. Сондықтан:

- **`python bot.py` (polling) Vercel-де мүлде істемейді.** Тек
  `TELEGRAM_MODE=webhook` арқылы жұмыс істейді.
- **`bot_state.pickle` тұрақты сақталмайды.** Vercel-дің файл жүйесі
  тек `/tmp`-ке жазуға рұқсат етеді, ол да cold start сайын өшіп кетеді.
  Яғни пайдаланушының тілі/тарихы уақыт өте келе жоғалуы мүмкін — бот
  құламайды, бірақ жады тұрақты емес. Толық шешім — сыртқы қойма (Redis,
  Vercel KV) қолдану, ол осы жобада әлі жоқ.
- **Webhook-ты бір рет қолмен тіркеу керек.** ASGI `lifespan` startup
  оқиғасы Vercel-де әр cold start сайын шақырыла бермейді, сондықтан
  автоматты тіркеуге сенбе:

  ```bash
  python scripts/register_webhook.py https://your-app.vercel.app
  ```

  Деплой URL-ы өзгерген сайын осыны қайта қос.

**Қадамдар:**

1. Vercel жоба баптауларында (Project → Settings → Environment Variables)
   мыналарды қос: `GEMINI_API_KEY`, `GEMINI_MODEL`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_MODE=webhook`, `TELEGRAM_WEBHOOK_URL` (өз Vercel домениің),
   `TELEGRAM_WEBHOOK_SECRET`, қажет болса `REGISTRATION_PORTAL_URL`.
2. Vercel-ге push жаса (`vercel.json` + `api/index.py` осы репода бар,
   Vercel оларды автоматты таниды).
3. Деплой аяқталғаннан кейін `python scripts/register_webhook.py
   https://<деплой-домені>` қос.
4. `https://<деплой-домені>/api/health` ашып, сервер тірі екенін тексер.

---

## User story-лерге сәйкестік

### US5 — AI чатбот

| Талап | Қайда орындалған |
|---|---|
| Еркін мәтінмен сұрақ | `telegram_bot.on_question`, `POST /api/chat` |
| Тек бекітілген білім қоры (RAG) | `core.SYSTEM_PROMPT` + `core.load_knowledge()` |
| Жауап табылмаса — fallback | Промпттағы ереже + `core.msg("no_answer")` |
| «Connection error» ескертуі | `core.MESSAGES["connection"]`, `_is_transport_error()` |
| 5 секунд ішінде жауап | `SLOW_RESPONSE_SECONDS` — әр жауап уақыты терминалда жазылады; `RESPONSE_BUDGET_SECONDS` — қатаң шек |

Терминалдағы лог тест есебіне тікелей қоюға жарайды:

```
[core] answered in 2.3s (target 5s)
[core] answered in 7.1s (target 5s)  <-- slower than the US5 target
```

### US3 — Расписание құру нұсқаулығы

`/guide` командасы `knowledge/registration_guide.<lang>.md` файлын көрсетеді.
Файл жоқ болса — «нұсқаулық әлі жарияланбаған, тіркеу бөліміне хабарласыңыз»
деп жауап береді, **басқа семестрдің нұсқаулығын көрсетпейді**.

Мерзімдер, пререквизиттер, дереккөз және жаңартылған күні — құжаттың өз
ішінде. Портал сілтемесі — `.env`-тегі `REGISTRATION_PORTAL_URL`.

### US6 — Үш тіл

Интерфейс (командалар, қателер, батырмалар) — қазақша / орысша / ағылшынша.
Бірінші рет `/start` басқанда тіл Telegram баптауынан алынады, сосын `/lang`
арқылы ауыстырылады. Gemini студент қай тілде жазса, сол тілде жауап береді —
құжаттар басқа тілде болса да.

## SDU жаңалықтары (University Announcements)

Telegram-да `/uni_announcments` немесе `/uni_announcements` командасын ашыңыз.
Тақырыпты батырмамен таңдауға болады. Мысалы, `/uni_announcments events`.
Бот пен веб-чат «Универде қандай жаңалықтар болып жатыр?», «Какие новости
в университете?» және “What's happening at SDU?” сияқты сұрақтарды сол тілде
жаңалықтар бөліміне бағыттайды.

- **Сайт:** [қазақша](https://sdu.edu.kz/feed/), [орысша](https://sdu.edu.kz/ru/feed/)
  және [ағылшынша](https://sdu.edu.kz/en/feed/) ресми RSS. Жазбалар жарияланған
  күнімен және түпнұсқа сілтемесімен көрсетіледі.
- **Әлеуметтік желілер:** Gemini Google Search арқылы SDU сайты сілтеме берген
  [Instagram](https://www.instagram.com/sdukz/) және
  [Facebook](https://www.facebook.com/sdukz) аккаунттарының индекстелген ашық
  жазбаларын іздейді. X/Twitter іздеуін қосу үшін ресми аккаунтты тексеріп,
  `SDU_X_HANDLE` мәнін `@` белгісінсіз жазыңыз. Іздеу тек индекстелген
  жарияланымдарды қамтиды. Instagram жазбасының авторы ашық embed арқылы
  расталуы керек. Google Search құралына қолдау көрсететін Gemini моделі
  қажет. Іздеу істемесе, RSS арқылы табылған жаңалықтар көрсетіледі.
- **Қолмен қосу:** команда мүшесі түпнұсқа посттың авторын, күнін және мәтінін
  тексергеннен кейін `SDU_CURATED_POSTS` айнымалысына JSON массивін қоса алады.
  Әр жазбада `lang` (`kk`, `ru`, `en`), `title`, `summary`, `date`
  (`YYYY-MM-DD`) және `url` (нақты посттың HTTPS сілтемесі) қажет.
  Әр тілдегі нұсқаны сол сілтемемен бөлек жазба ретінде беріңіз.

### Жазылым мен хабарлама

1. Redis REST (мысалы, Upstash) үшін `ANNOUNCEMENTS_REDIS_URL` және
   `ANNOUNCEMENTS_REDIS_TOKEN` орнатыңыз. Жазылым батырмалары боттың жеке
   чатында пайда болады. Жазылым Telegram аккаунтына байланысты.
2. Деплойға `CRON_SECRET` қосыңыз. Vercel-де тапсырма күн сайын 04:00 UTC
   (Алматы уақытымен 09:00) орындалады. Басқа хостта scheduler арқылы
   `GET /api/announcements/notify` сұрауын жіберіңіз. Сұрауға
   `Authorization: Bearer <CRON_SECRET>` тақырыбын қосыңыз.
3. Telegram мәзірін жаңарту үшін Vercel деплойынан кейін
   `python scripts/register_webhook.py https://your-app.vercel.app` іске
   қосыңыз. Polling режимінде мәзір бот іске қосылған кезде тіркеледі.

Хабарламалар жазылған уақыттан кейін шыққан материалдар үшін жіберіледі.
Уақыты көрсетілмеген әлеуметтік желі жазбалары үшін келесі жергілікті күннен
бастап жарияланған материалдар есептеледі. Бір постқа бірнеше тақырып сай
келсе, бот оның сілтемесі бойынша хабарламаны қайталап жібермеуге тырысады.
