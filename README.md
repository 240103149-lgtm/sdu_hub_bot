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
| `/deadline` (`/deadlines`) | Moodle дедлайндары, аккаунтты байланыстыру | — |
| `/events` | Жарияланған кампус ивенттері, күні және мәртебесі | — |
| `/rooms` (`/room`) | Бос кабинеттер; `/rooms 14:30`, күн/уақыт батырмалары | — |
| `/grade` | Порталдағы бағалар | — |
| `/unlink` | Портал байланысын өшіру | — |
| `/unlink_moodle` | Moodle байланысын өшіру | — |
| `/cancel` | Аккаунтқа кіруді тоқтату | — |
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

## Біріктірілген бот

Барлық команда бір `TELEGRAM_BOT_TOKEN` және бір `build_application()` арқылы
жұмыс істейді. Іске қосу: `python bot.py`; webhook режимінде `main.py` сол
ботты қолданады. Қосымша бот процесін іске қосудың қажеті жоқ.

- `/deadline`: Moodle аккаунтына жеке чатта кіру, дедлайндарды мерзімі бойынша
  көрсету. Пароль сақталмайды; сессия ғана сақталады. `/unlink_moodle` оны өшіреді.
- `/events`: `knowledge/campus_events.md` ішіндегі барлық жарияланған ивент.
  Мәртебесі кампус күнтізбесі бойынша есептеледі. Конференцияның өтініш дедлайны
  мен ивенттің күні бөлек көрсетіледі. Мәлімет түпнұсқаның тілінде, мәртебе мен
  интерфейс `/lang` тілінде. Бұл команда Gemini сұрауын жібермейді.
- `/rooms` немесе `/room`: `data/schedule.json` кестесі бойынша бос кабинеттер.
  `/rooms 14:30` және күн/сабақ уақыты батырмалары бар. Бұл тек сабақ кестесінің
  есебі: нақты броньдар мен аудиторияның физикалық қолжетімділігі кірмейді.
  Жаңа семестрде кесте файлын жаңарту керек.
- `/grade` пен `/deadline` ортақ кіру диалогын қолданады. Бірінен екіншісіне
  ауысқанда алдыңғы кіру тоқтайды. `/events`, `/rooms`, `/help` сияқты басқа
  командалар да кіруді аяқтап, өз әрекетін орындайды. `/cancel` — тоқтату.

Тексеру (нақты аккаунт пен API кілтінсіз, сыртқы сервистерге сұраусыз):

```bash
python -m unittest discover -s tests -v
```

Commit/branch дереккөздері мен өзгерістер: [INTEGRATION.md](INTEGRATION.md).
