# University AI Knowledge Hub — Telegram бот + сайт

Бір білім қоры, бір промпт, екі интерфейс:

```
core.py            ← бүкіл логика: knowledge/ оқу, промпт, қателер, тілдер
ai.py              ← AI модельдері: Gemini, DeepSeek, OpenAI форматындағы кез келген API
rooms.py           ← бос кабинеттер: data/schedule.json бойынша есептеу
grade.py           ← университет порталы (my.sdu.edu.kz)
├── telegram_bot.py  Telegram боты (негізгі handler-лер)
│   ├── grade_bot.py   /grade ┐ функциялар — бөлек модульдер,
│   ├── rooms_bot.py   /rooms ┘ build_application()-да тіркеледі
│   └── bot.py       polling режимінде іске қосу  →  python bot.py
└── main.py          сайт + REST API (+ webhook)  →  uvicorn main:app --reload
```

Бот пен сайт бір `core.py`-ды (және `rooms.py`-ды) қолданады, сондықтан екеуі
әрқашан **бірдей құжаттардан бірдей жауап** береді. Жаңа мүмкіндікті бір жерге қоссаң,
екеуінде де жұмыс істейді. Қай AI жауап беретіні кодқа емес, `.env`-ке байланысты
(1.2-бөлім).

---

## 1. Орнату

```bash
pip install -r requirements.txt
cp env.example .env        # Windows: copy env.example .env
```

`.env` файлын ашып толтыр:

| Айнымалы | Қайдан аласың |
|---|---|
| `AI_PROVIDER` | `gemini` (әдепкі), `deepseek` немесе `openai` — 1.2-бөлім |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey (Gemini таңдалса) |
| `DEEPSEEK_API_KEY` | https://platform.deepseek.com/api_keys (DeepSeek таңдалса) |
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

## 1.2. AI моделін таңдау (Gemini, DeepSeek, ...)

Студенттерге қай модель жауап беретінін `.env`-тегі `AI_PROVIDER` шешеді —
кодты өзгертудің қажеті жоқ:

| `AI_PROVIDER` | `.env`-те не керек |
|---|---|
| `gemini` (әдепкі) | `GEMINI_API_KEY`, қаласаң `GEMINI_MODEL` (әдепкі `gemini-flash-latest`) |
| `deepseek` | `DEEPSEEK_API_KEY`, қаласаң `DEEPSEEK_MODEL` (әдепкі `deepseek-flash`) |
| `openai` | `OPENAI_API_KEY`, `OPENAI_MODEL`, басқа сервис болса `OPENAI_BASE_URL` |

Бірнешеуін үтірмен жазсаң, кезекпен қолданылады:

```ini
AI_PROVIDER=gemini,deepseek   # Gemini лимитке жетсе не құласа, DeepSeek жауап береді
```

- `openai` — тек OpenAI емес, OpenAI форматындағы кез келген API: мысалы
  OpenRouter үшін `OPENAI_BASE_URL=https://openrouter.ai/api/v1`, өз
  компьютеріңдегі Ollama үшін `http://localhost:11434/v1`.
- Әр провайдердің `<АТЫ>_FALLBACK_MODEL`-і болуы мүмкін: негізгі модель
  жүктемеде болса, алдымен сол қолданылады.
- DeepSeek әдепкіде «ойланбай» (thinking режимінсіз) жауап береді — бұл
  бірнеше есе жылдам, ал US5 жауапты 5 секундта талап етеді.
- Қай модельдер қолданылатыны `/api/health`-тағы `ai` тізімінде көрінеді.
  Біреуі құлап, келесісі жауап берсе, терминалда `[ai] answered by ...` шығады.

**Жаңа провайдер қосу** (`ai.py`):

- OpenAI форматында болса — `PROVIDERS` сөздігіне бір жазба (`base_url` пен
  әдепкі `model`), басқа код керек емес. Сосын `.env`: `<АТЫ>_API_KEY=...`,
  `AI_PROVIDER=<аты>`.
- Өз API-і болса — `complete()` әдісі бар класс (үлгі: `Gemini`) және
  `_build()`-та бір тармақ.

## 1.5. Тексеру

```bash
python selftest.py
```

Кітапханалар, `.env`, `knowledge/`, нұсқаулық, AI жауабының уақыты, бот
токені және сабақ кестесі — бәрі бір команда мен тексеріледі. Демо алдында
осыны қос.

## 2. Білім қорын толтыру

`knowledge/` папкасына университеттің бекітілген құжаттарын сал (`.md`,
`.txt`, `.docx`). Толық нұсқаулық — `knowledge/README.md`.

Файлдар өзгергенде автоматты түрде қайта оқылады — серверді қайта қосудың
қажеті жоқ.

## 2.5. Сабақ кестесі (бос кабинеттер)

Боттағы `/rooms` пен сайттағы «Бос кабинеттер» беті `data/schedule.json`
бойынша есептеледі: сол уақытта кабинетте сабақ болмаса, ол бос. Әр
кабинеттің жанында ондағы келесі сабақтың уақыты жазылады — кабинет сол
уақытқа дейін бос.

Файл — порталдан алынған сабақтар тізімі, әр сабақ бір жол:

```json
{"day": "Mo", "start": "08:30", "end": "09:20", "room": "G102",
 "building": "Main building - Engineering block", "virtual": false}
```

- Виртуал сабақтар (`"virtual": true`) мен спорт кешені есепке кірмейді —
  олар отыруға болатын кабинет емес.
- `"I 110, I 111"` — бір сабақ екі кабинетте: екеуі де бос емес деп саналады.
- «Қазір» Қазақстан уақытымен (UTC+5) есептеледі, сервер қай елде тұрса да.
- Жаңа семестрде файлды жаңасымен ауыстыр — бот пен сайт оны қайта қоспай-ақ
  оқиды.

> ⚠️ Кабинет **тек файлдағы** сабақтар бойынша бос деп саналады. Файлда
> барлық факультеттің барлық секциясы болмаса, басқа сабақтар жүріп жатқан
> кабинеттер бос болып көрінеді — сондықтан толық кестені сал.

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
| `/grade` | Университет порталын байланыстырып, бағаларды көрсетеді (`/unlink` — ажырату) | — |
| `/rooms` | Қазір бос кабинеттер; күн мен уақыт батырмамен ауысады, `/rooms 14:30` те болады | — |
| `/lang` | Қазақша / Русский / English | US6 |
| `/reset` | Әңгіме тарихын тазартады | — |
| `/help` | Анықтама | — |

Тіл мен әңгіме тарихы әр қолданушыға бөлек сақталады (`bot_state.pickle`),
сондықтан ботты қайта қосқанда да жоғалмайды.

### Ботқа жаңа функция қосу

Әр үлкен функция — бөлек модуль. Логикасы `<аты>.py`-да (Telegram-ды да,
сайтты да білмейді, мысалы `rooms.py`), бот бөлігі `<аты>_bot.py`-да (мысалы
`rooms_bot.py`), ол бір ғана `register_<аты>(app)` функциясын береді:

```python
def register_rooms(app):
    add_command("rooms", MENU, HELP_LINE)  # "/" мәзірі мен /help, үш тілде
    app.add_handler(CommandHandler("rooms", cmd_rooms))
```

Сосын `telegram_bot.build_application()`-ға екі жол: import пен шақыру.
`vercel.json` барлық `*.py` файлды өзі алады, оны өзгертудің қажеті жоқ.

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
| `GET /api/rooms?day=Mo&time=10:30` | бос кабинеттер (параметрсіз — қазіргі сабақ уақыты) |
| `GET /api/health` | сервер тірі ме, қандай құжаттар мен неше кабинет көрінеді |

`/api/health` — демо алдында тексеруге ыңғайлы: `documents` тізімі бос болса,
`knowledge/` папкасы бос деген сөз; `rooms` `null` болса, `data/schedule.json`
оқылмаған.

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
   мыналарды қос: `AI_PROVIDER` пен соған керек кілт (мысалы `GEMINI_API_KEY`
   немесе `DEEPSEEK_API_KEY`), `TELEGRAM_BOT_TOKEN`,
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
| «Connection error» ескертуі | `core.MESSAGES["connection"]`, `ai.ProviderError("connection")` |
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
арқылы ауыстырылады. AI студент қай тілде жазса, сол тілде жауап береді —
құжаттар басқа тілде болса да.
