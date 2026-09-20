# 🎓 SDU AI Knowledge Hub & Student Assistant

![SDU Logo](https://img.shields.io/badge/SDU-University-blue?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-In_Development-orange?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

**SDU AI Knowledge Hub** — Сүлеймен Демирел Университеті (SDU) студенттеріне арналған AI негізіндегі кешенді ақпараттық-көмекші жүйе. Жүйе RAG (Retrieval-Augmented Generation) технологиясы мен AI чат-ботын қолдану арқылы академиялық үдерістерді, кампус жаңалықтарын, бос аудиторияларды табуды, құжаттарға тапсырыс беруді және басқа да қызметтерді автоматтандыруға бағытталған.

---

## 👥 Команда мүшелері (Team Members)

| № | Студенттің Аты-жөні | Student ID | Жауапты бағыты / Өңдейтін тапсырмалары |
|---|---------------------|------------|-----------------------------------------|
| 1 | **Yengsebek Zhannur** | `240103149` | Free-format AI Chatbot, Multi-language AI, System Authentication |
| 2 | **Rassul Kanatbek** | `240103142` | Tuition Payments, Scholarship Requirements, Certificate Requests |
| 3 | **Aiaru Batan** | `240103092` | Available Classrooms Search, Campus Events Guide |
| 4 | **Bakytkerey Abulsagit** | `240103179` | Document Submission, Student ID Request, Dormitory Rules |
| 5 | **Beisembek Ali** | `240103097` | Course Registration Guide, View Grades & Transcript |

---

## 🚀 Жобаның басты мүмкіндіктері (Key Features)

- **🤖 AI Чат-бот (Free-format AI Queries):** Еркін мәтін форматында SDU ережелері мен академиялық процестері бойынша сұрақтарға лезде жауап беру.
- **🌍 Көптілділік (Multi-language Support):** Қазақ, орыс және ағылшын тілдерінде толыққанды AI диалогы.
- **🔐 SSO Аутентификация (Student Authentication):** Университет берген ID мен пароль арқылы жүйеге қауіпсіз кіру.
- **🏫 Бос аудиторияларды табу (Find Classrooms):** Жобалық жұмыстар мен өздік дайындық үшін кампустағы бос бөлмелерді орналасқан уақыты бойынша іздеу.
- **📚 Академиялық прогресс мен тіркеу:** Пәндерге тіркелу нұсқаулығы (Course Registration Guide), семестрлік бағалар мен транскриптті қарау.
- **📜 Цифрлық қызметтер:** Анықтама алу (Certificate Request), студенттік билетке өтініш беру, құжат тапсыру.
- **💳 Қаржылық және Жатақхана ақпараты:** Оқу ақысын төлеу реквизиттері, шәкіртақы шарттары (Scholarship) мен жатақхана ережелері.

---

## 📋 User Stories & Орындау кестесі (Project Schedule)

| ID | User Story Title | Story Description | Estimation (SP) | Responsible Person | Iteration | Deadline Schedule |
|:--:|------------------|-------------------|:---------------:|--------------------|:---------:|:-----------------:|
| **US1** | Free-format AI Chatbot Query | As a student, I want to ask questions to the AI chatbot in free text format to quickly find university answers. | 5 | **Yengsebek Zhannur** | 1 | 14.09.2026 – 21.09.2026 |
| **US2** | View Course Registration Guide | As a student, I want to view step-by-step course registration instructions, deadlines, and prerequisites. | 2 | **Beisembek Ali** | 2 | 14.09.2026 – 21.09.2026 |
| **US3** | Find Available Classrooms | As a student, I want to see which classrooms are available to study or work on group projects. | 5 | **Aiaru Batan** | 1 | 21.09.2026 – 28.09.2026 |
| **US4** | View Campus Events | As a student, I want to view the university event schedule so that I can plan which events to attend. | 4 | **Aiaru Batan** | 2 | 21.09.2026 – 28.09.2026 |
| **US5** | View Grades and Academic Transcript | As a student, I want to view my grades by semester and transcript to track my academic progress. | 4 | **Beisembek Ali** | 3 | 21.09.2026 – 28.09.2026 |
| **US6** | Multi-language AI Interaction | As a user, I want to interact with the AI Knowledge Hub in Kazakh, Russian, or English. | 3 | **Yengsebek Zhannur** | 4 | 05.10.2026 – 12.10.2026 |
| **US7** | Student System Authentication | As a student, I want to securely log in using my university ID and password. | 3 | **Yengsebek Zhannur** | 4 | 12.10.2026 – 19.10.2026 |
| **US8** | Document Submission | As a new student, I want to submit required documents to complete enrollment. | 5 | **Bakytkerey Abulsagit** | 5 | 19.10.2026 – 26.10.2026 |
| **US9** | Student ID Card Request | As a new student, I want to apply for a student ID card to access campus facilities. | 3 | **Bakytkerey Abulsagit** | 5 | 26.10.2026 – 02.11.2026 |
| **US10** | Dormitory Rules Acknowledgment | As a student, I want to read dormitory placement rules to properly settle in. | 2 | **Bakytkerey Abulsagit** | 6 | 02.11.2026 – 09.11.2026 |
| **US11** | View Tuition Payment Details | As a student, I want to find payment requisites and instructions for paying tuition fees. | 3 | **Rassul Kanatbek** | 6 | 09.11.2026 – 16.11.2026 |
| **US12** | Check Scholarship Requirements | As a student, I want to read scholarship eligibility requirements before the selection period. | 3 | **Rassul Kanatbek** | 7 | 16.11.2026 – 23.11.2026 |
| **US13** | Request a Certificate | As a student, I want to submit a request for an official enrollment certificate online. | 4 | **Rassul Kanatbek** | 7 | 23.11.2026 – 30.11.2026 |

---

## 🛠 Технологиялық стек (Tech Stack)

- **Frontend:** React.js / Next.js, Tailwind CSS
- **Backend:** Python (FastAPI / Django)
- **AI & RAG Engine:** LangChain / LlamaIndex, OpenAI API / Local LLM, Vector Database (ChromaDB / PgVector)
- **Database:** PostgreSQL
- **Authentication:** OAuth2 / SDU SSO System
- **Version Control:** Git, GitHub / GitLab

---

## 💻 Жобаны жергілікті компьютерде іске қосу (Local Setup)

### 1. Репозиторийді клондыу (Clone Repository)
```bash
git clone https://github.com/your-username/sdu-ai-knowledge-hub.git
cd sdu-ai-knowledge-hub
```

### 2. Бекэндті баптау (Backend Setup)
```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows үшін: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 3. Фронтэндті баптау (Frontend Setup)
```bash
cd frontend
npm install
npm run dev
```

Жоба жергілікті мекенжайда ашылады: `http://localhost:3000`

---

## ✉️ Контактілер

Кез келген сұрақтар мен ұсыныстар бойынша SDU студенттік командасына хабарласа аласыз.

- **Университет:** SDU University (Suleyman Demirel University)
- **Жоба:** SDU AI Knowledge Hub System