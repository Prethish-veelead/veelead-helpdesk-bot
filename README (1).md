# Veelead Helpdesk Bot — Complete Documentation

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Cost Breakdown](#3-cost-breakdown)
4. [How SharePoint Access Works (Cross-Tenant)](#4-how-sharepoint-access-works-cross-tenant)
5. [AI Models — Embedding and Chat](#5-ai-models--embedding-and-chat)
6. [How Documents Get Synced Automatically](#6-how-documents-get-synced-automatically)
7. [Azure Hosting Setup](#7-azure-hosting-setup)
8. [Environment Variables](#8-environment-variables)
9. [Project Folder Structure](#9-project-folder-structure)
10. [Local Development Setup](#10-local-development-setup)
11. [Deployment Flow (GitHub Actions)](#11-deployment-flow-github-actions)
12. [API Endpoints (for Frontend Developers)](#12-api-endpoints-for-frontend-developers)
13. [Frontend (chatbot.html)](#13-frontend-chatbothtml)
14. [Security Model](#14-security-model)
15. [Monitoring and Operations](#15-monitoring-and-operations)
16. [Troubleshooting](#16-troubleshooting)
17. [Capabilities and Limits](#17-capabilities-and-limits)

---

## 1. Project Overview

The **Veelead Helpdesk Bot** is an AI-powered chatbot that answers employee questions by searching the company's SharePoint knowledge base. Employees ask natural-language questions like "How do I reset my password?" and the bot returns clear answers with source citations from official company documents.

### Key features
- Reads policies, IT guides, and HR documents directly from SharePoint
- Answers using AI but only from approved company documents (no hallucination)
- Cites the source document and page number with every answer
- Automatically detects new/updated/deleted documents in SharePoint
- Caches frequent questions to keep costs low
- Filters by category (IT, HR, Facilities, etc.) for precise results
- Suggests follow-up questions

### Hosted URL
`https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net`

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                                                          │
│   EMPLOYEE'S BROWSER                                                     │
│   ┌────────────────────────┐                                             │
│   │ chatbot.html (frontend)│                                             │
│   └───────────┬────────────┘                                             │
│               │ HTTPS + x-api-key header                                 │
│               ↓                                                          │
│   ┌─────────────────────────────────────────────────────────┐            │
│   │ Azure App Service: veelead-helpdesk-bot                 │            │
│   │ (Python 3.11 + FastAPI, Linux B1)                       │            │
│   │                                                         │            │
│   │  /search.json  /categories  /health  /admin/*           │            │
│   │                                                         │            │
│   │  Internal pipeline:                                     │            │
│   │   1. Cache lookup (SQLite)                              │            │
│   │   2. Classifier (gpt-4o-mini)                           │            │
│   │   3. Embed query (text-embedding-3-small)               │            │
│   │   4. Search index (Azure AI Search)                     │            │
│   │   5. Generate answer (gpt-4o-mini / gpt-4o)             │            │
│   │                                                         │            │
│   │  Background scheduler: hourly SharePoint sync           │            │
│   └───────┬──────────────┬──────────────┬────────────┬──────┘            │
│           │              │              │            │                   │
│           ↓              ↓              ↓            ↓                   │
│   ┌──────────────┐ ┌──────────────┐ ┌────────────┐ ┌────────────────┐    │
│   │ Azure OpenAI │ │ Azure AI     │ │ SQLite     │ │ SharePoint     │    │
│   │ Embeddings + │ │ Search       │ │ (cache +   │ │ Helpdesk Site  │    │
│   │ Chat         │ │ (index)      │ │ sync state)│ │ (veeleaddev    │    │
│   │              │ │              │ │            │ │  tenant)       │    │
│   └──────────────┘ └──────────────┘ └────────────┘ └────────────────┘    │
│   (veelead.com tenant)              (App Service   (different Microsoft  │
│                                      /home folder)  tenant — see §4)     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Tech stack
- **Runtime**: Python 3.11 on Linux
- **Web framework**: FastAPI + Gunicorn + Uvicorn
- **Hosting**: Azure App Service (Linux Basic B1)
- **Vector store**: Azure AI Search (Free tier → Basic at scale)
- **AI**: Azure OpenAI (gpt-4o-mini, gpt-4o, text-embedding-3-small)
- **Document source**: SharePoint via Microsoft Graph API
- **Local cache**: SQLite files in `/home/data`
- **Auth**: API key (header `x-api-key`)

---

## 3. Cost Breakdown

Real numbers, projected by document count and query volume.

### 🎯 Important: Azure AI Search is on FREE tier right now

The bot currently uses **Azure AI Search Free tier — costing $0/month**.

| What you get on Free tier | Limit |
|---|---|
| Storage | 50 MB |
| Documents (chunks) | Up to 10,000 |
| Indexes | Up to 3 |
| Cost | **$0 forever** (as long as you're under limits) |

**You stay on Free tier until ~300 SharePoint documents.** At that point, the index hits the 50 MB storage limit and you upgrade to **Basic tier ($25/month)** with one click in the Azure portal — no code changes, no migration, no downtime.

For reference: 14 documents currently use ~3 MB. You have plenty of headroom.

### Fixed costs (independent of usage)

| Component | Tier | Cost/month |
|---|---|---|
| Azure App Service (Linux B1) | 1 vCPU, 1.75GB RAM | **$13.14** |
| Azure AI Search (Free tier) ✅ **current** | 50MB storage, ~300 docs max | **$0** |
| Azure AI Search (Basic tier) — future | 15GB storage, needed past 300 docs | $25 |

### Variable costs (usage-based)

**Azure OpenAI — Embeddings** (`text-embedding-3-small`)
- Price: **$0.02 per 1 million tokens**
- One-time cost for initial document indexing
- Recurring cost only when documents are added/updated

**Azure OpenAI — Chat** (`gpt-4o-mini` for ~80% of queries)
- Input: **$0.15 per 1M tokens**
- Output: **$0.60 per 1M tokens**
- Typical query: ~3000 input tokens + ~300 output tokens = $0.00063 per query

**Azure OpenAI — Chat** (`gpt-4o` for complex queries, ~20%)
- Input: **$2.50 per 1M tokens**
- Output: **$10 per 1M tokens**
- Typical query: ~3000 input + ~500 output = $0.0125 per query

### Projected monthly cost at different scales

| Scale | Docs | Queries/mo | Hosting | Search | OpenAI | **TOTAL** |
|---|---|---|---|---|---|---|
| **Now (launch)** | 14 | 1,000 | $13 | $0 (Free) | $5 | **~$18/mo** |
| **6 months** | 100 | 3,000 | $13 | $0 (Free) | $8 | **~$21/mo** |
| **1 year** | 300 | 5,000 | $13 | $0 (Free) | $12 | **~$25/mo** |
| **2 years** | 500 | 8,000 | $13 | $25 (Basic) | $18 | **~$56/mo** |
| **Heavy** | 1,000 | 15,000 | $13 | $25 (Basic) | $25 | **~$63/mo** |

### Cost-saving features built in
- **Query cache (7-day TTL)** — repeat questions cost $0
- **Model routing** — uses cheaper gpt-4o-mini for ~80% of queries
- **Category filtering** — searches a smaller subset, fewer tokens to LLM
- **Delta sync** — only re-embeds changed documents, not all of them

### Comparison with alternatives

| Solution | Cost for 50 employees |
|---|---|
| Microsoft 365 Copilot | $30/user × 50 = **$1,500/month** |
| Enterprise search (Glean, Coveo) | **$30,000+/year** |
| **This bot** | **~$25/month** at production scale |

---

## 4. How SharePoint Access Works (Cross-Tenant)

This is critical to understand for anyone troubleshooting auth issues.

### The situation

There are **two separate Microsoft 365 tenants** involved:

| Tenant | Used for |
|---|---|
| `veelead.com` | Azure resources (App Service, OpenAI, AI Search) — Prethish's account |
| `veeleaddev.onmicrosoft.com` | SharePoint Helpdesk site — Cynthia's account |

These are completely separate organizations from Microsoft's perspective. A user in one tenant cannot directly access resources in the other tenant.

### How the bot bridges the two tenants

The bot uses an **Azure AD App Registration** created **in the SharePoint tenant** (`veeleaddev.onmicrosoft.com`) by Cynthia. This app registration is a "service identity" that:
- Lives in the SharePoint tenant
- Has read-only permission to the SharePoint library (`Sites.Read.All` + `Files.Read.All`)
- Has admin consent granted

The bot uses this app registration's credentials (`TENANT_ID` + `CLIENT_ID` + `CLIENT_SECRET`) to authenticate to SharePoint via the Microsoft Graph API.

```
Azure App Service (veelead.com tenant)
        │
        │ uses these credentials:
        │   TENANT_ID:     veeleaddev's tenant ID
        │   CLIENT_ID:     from app registration
        │   CLIENT_SECRET: from app registration
        ↓
Microsoft Graph API (graph.microsoft.com)
        │
        │ authenticates AS the app registration
        ↓
SharePoint Helpdesk Library
   (HD_KnowledgeDocuments in veeleaddev tenant)
```

### What the bot CAN do in SharePoint
- ✅ Read files (PDF, DOCX, HTML)
- ✅ Read list metadata (Category, Tags, Status, Article Title, etc.)
- ✅ Detect changes via delta query

### What the bot CANNOT do
- ❌ Modify or delete files
- ❌ Access SharePoint sites other than the one configured
- ❌ Read user profiles, emails, or anything outside the Helpdesk site

### How to revoke access
Cynthia (or any admin in `veeleaddev.onmicrosoft.com`) can delete the app registration at any time. This immediately stops the bot from accessing SharePoint.

### Setting up the app registration (one-time, done already)

Admin in `veeleaddev.onmicrosoft.com` should:
1. Open Azure portal → switch to `veeleaddev` directory
2. Search **Microsoft Entra ID** → **App registrations** → **New registration**
3. Name: `Veelead-Helpdesk-RAG-Bot`, single tenant
4. After creation, copy **Application (client) ID** and **Directory (tenant) ID**
5. Go to **Certificates & secrets** → **New client secret** (24 months)
6. Immediately copy the secret value (it disappears after page refresh)
7. Go to **API permissions** → add Microsoft Graph **Application** permissions:
   - `Sites.Read.All`
   - `Files.Read.All`
8. Click **Grant admin consent** at the top

The 3 values (TENANT_ID, CLIENT_ID, CLIENT_SECRET) go into the bot's environment variables.

---

## 5. AI Models — Embedding and Chat

The bot uses three Azure OpenAI models for three different jobs.

### Model 1: `text-embedding-3-small` (Embedding model)

**What it does:** Converts text into a 1536-dimensional numeric vector. Used for semantic search.

**Where it's used:**
- **During indexing** — every document chunk gets embedded once and stored in Azure AI Search
- **During query** — the user's question gets embedded so we can find similar chunks

**Why this model:**
- Cheap: $0.02 per 1M tokens
- Fast: ~200ms per call
- Quality: sufficient for company documents (the bigger `text-embedding-3-large` adds 3% accuracy for 6× the cost)

**Configuration:**
```
EMBED_DEPLOYMENT=text-embedding-3-small
EMBED_API_VER=2024-12-01-preview
EMBED_ENDPOINT=https://policyveelead123.openai.azure.com/
```

### Model 2: `gpt-4o-mini` (Default chat model)

**What it does:** Reads the user's question + retrieved document chunks, generates the natural-language answer.

**Used for:** ~80% of queries — anything that's a normal helpdesk question.

**Why this model:**
- Cheap: $0.15 input / $0.60 output per 1M tokens
- Fast: ~1-2 seconds per answer
- Quality: excellent for factual extraction from documents

**Configuration:**
```
GPT_MINI_DEPLOY=gpt-4o-mini
GPT_API_VER=2024-12-01-preview
GPT_ENDPOINT=https://sharepointbot.openai.azure.com/
```

### Model 3: `gpt-4o` (Complex query model)

**What it does:** Same as `gpt-4o-mini` but for queries needing deeper reasoning.

**Used for:** ~20% of queries — those that involve:
- Comparison ("compare HR policy A vs policy B")
- Synthesis across multiple documents
- Long queries (>30 words)
- Keywords like: compare, vs, analyze, summarize, step-by-step

**The bot decides automatically** via this rule (in `app.py`):
```python
if len(question.split()) > 30 or COMPLEX_QUERY_RE.search(question):
    return settings.gpt_large_deploy  # gpt-4o
return settings.gpt_mini_deploy        # gpt-4o-mini
```

**Why split into two models:**
- Cost: `gpt-4o` is ~16× more expensive than `gpt-4o-mini`
- Using only `gpt-4o` would multiply monthly OpenAI costs ~5×
- Using only `gpt-4o-mini` slightly reduces quality on complex analytical questions

**Configuration:**
```
GPT_LARGE_DEPLOY=gpt-4o
```

### One LLM call produces TWO outputs

To save tokens, the bot asks the LLM to return BOTH the answer AND follow-up suggestions in a single call (using `response_format={"type": "json_object"}`). The model returns:

```json
{
  "answer": "...",
  "suggested_followups": ["Q1", "Q2", "Q3"]
}
```

This is more efficient than calling the LLM twice.

---

## 6. How Documents Get Synced Automatically

This is the most-asked stakeholder question. Here's the complete answer.

### Microsoft Graph Delta Query

The bot uses Microsoft Graph's **delta query** feature. Think of it as a "bookmark" — SharePoint tracks every change and gives the bot a token that says "everything up to this point."

### Sync timeline

```
Day 1, first startup:
  Bot → SharePoint: "Give me everything you have"
  SharePoint → Bot: "Here are all 14 files. Bookmark: TOKEN_A"
  Bot stores TOKEN_A in SQLite
  Bot indexes all 14 files in Azure AI Search

1 hour later:
  Scheduler triggers run_sync()
  Bot → SharePoint: "What's changed since TOKEN_A?"
  SharePoint → Bot: "Nothing. New bookmark: TOKEN_B"
  Bot updates TOKEN_B in SQLite

Day 2, 10:00 AM — Cynthia uploads new policy and updates HR doc:
  10:00 AM: Cynthia's actions happen in SharePoint
  10:00–11:00 AM: Bot still serves answers from old version (acceptable lag)
  11:00 AM: Scheduler triggers
  Bot → SharePoint: "What's changed since TOKEN_B?"
  SharePoint → Bot:
    ADDED:    Travel_Policy.pdf
    UPDATED:  HR_Policy_V1.4.pdf
    DELETED:  old-draft-id
    New bookmark: TOKEN_C
  Bot processes only these 3 items (not all 15 files)
  Bot stores TOKEN_C
  Bot is now serving fresh answers
```

### What the bot does for each change

**For new files:**
1. Download file content from SharePoint
2. Extract text (PDF/DOCX/HTML)
3. Chunk into ~400-word pieces
4. Generate embeddings via `text-embedding-3-small`
5. Upload to Azure AI Search index

**For updated files:**
1. Delete OLD chunks from the index (by file ID)
2. Same steps as "new files" with the new content
3. **Clear cached answers** that cited this file (so users get fresh content next time they ask)

**For deleted files:**
1. Remove all chunks from the index
2. Clear cached answers citing this file

### What does and doesn't get indexed

✅ **Indexed:** Files with **Status = "Published"** or **"Approved"**
❌ **Skipped:** Drafts, In Review, etc.

This is controlled by the `ArticleStatus` column in the SharePoint list. When a draft becomes Published, the next hourly sync picks it up automatically.

### Sync configuration

```bash
SYNC_INTERVAL_MINUTES=60   # adjust to 15 or 30 for faster updates
```

### Manual sync trigger

If you need a doc indexed faster than waiting for the hourly sync:

```bash
curl -X POST -H "x-api-key: <KEY>" \
  https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net/admin/reindex
```

### Force full re-sync (if delta gets stuck)

```bash
curl -X POST -H "x-api-key: <KEY>" \
  https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net/admin/reset_sync

# Then trigger reindex
curl -X POST -H "x-api-key: <KEY>" \
  https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net/admin/reindex?force_full=true
```

---

## 7. Azure Hosting Setup

### Resource group structure

```
veelead.com Azure subscription
└── Resource Group: rg-veelead-helpdesk
    ├── App Service Plan: asp-veelead-helpdesk-bot (Linux B1)
    ├── App Service: veelead-helpdesk-bot (Python 3.11)
    ├── Azure AI Search: sharepointbot (Free tier — $0/mo, upgrade to Basic at ~300 docs)
    └── (Existing Azure OpenAI resources:
         policyveelead123 — for embeddings
         sharepointbot — for chat)
```

### Critical App Service settings

| Setting | Value |
|---|---|
| Operating system | Linux |
| Runtime | Python 3.11 |
| Plan | Linux Basic B1 |
| Always On | **ON** (critical — prevents cold starts) |
| HTTPS Only | ON |
| FTP State | Disabled (security) |
| Minimum TLS | 1.2 |

### Startup command

In **Configuration → General settings → Startup Command**:

```bash
gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 app:app
```

Use 2 workers for B1 (1.75GB RAM). Bump to 4 if upgrading to B2 or higher.

### Persistent storage

The bot stores SQLite databases in the `/home/data` directory:
- `cache.db` — query cache
- `sync_state.db` — delta tokens + sync audit log

This directory survives App Service restarts. Set in `.env`:
```bash
DATA_DIR=/home/data
```

For local dev, use `DATA_DIR=./data` instead.

### Budget alert (highly recommended)

In Azure portal → **Cost Management** → **Budgets**:
- Create budget: $60/month threshold
- Alert at 80%, 100%, 120%
- Notify: `prethish.g@veelead.com`

This protects against runaway costs from misconfigured API keys or abuse.

---

## 8. Environment Variables

All values are set in Azure portal → App Service → **Environment variables** → **App settings**.

### Quick reference

| Variable | Example value | Notes |
|---|---|---|
| `SOURCE_TYPE` | `sharepoint` | `local` for testing, `sharepoint` for production |
| `TENANT_ID` | `08a7d6c3-ef04-...` | **veeleaddev tenant ID** (NOT veelead.com) |
| `CLIENT_ID` | `95e38cb9-5cd8-...` | From app registration |
| `CLIENT_SECRET` | `92P8Q~IRa4_x...` | Sensitive — never log |
| `SHAREPOINT_SITE_URL` | `https://veeleaddev.sharepoint.com/sites/Helpdesk` | Full site URL |
| `SHAREPOINT_LIBRARY` | `HD_KnowledgeDocuments` | Display name of library |
| `EMBED_ENDPOINT` | `https://policyveelead123.openai.azure.com/` | Trailing slash matters |
| `EMBED_API_KEY` | (sensitive) | From Azure OpenAI Keys |
| `EMBED_DEPLOYMENT` | `text-embedding-3-small` | Deployment name |
| `EMBED_API_VER` | `2024-12-01-preview` | |
| `GPT_ENDPOINT` | `https://sharepointbot.openai.azure.com/` | |
| `GPT_API_KEY` | (sensitive) | |
| `GPT_MINI_DEPLOY` | `gpt-4o-mini` | |
| `GPT_LARGE_DEPLOY` | `gpt-4o` | |
| `GPT_API_VER` | `2024-12-01-preview` | |
| `SEARCH_ENDPOINT` | `https://sharepointbot.search.windows.net` | |
| `SEARCH_API_KEY` | (sensitive) | Admin key from Azure AI Search |
| `SEARCH_INDEX` | `veelead-docs` | |
| `API_KEY` | `veelead-secure-9f...` | Generate strong random; sent in `x-api-key` header |
| `CHUNK_SIZE` | `400` | Words per chunk |
| `CHUNK_OVERLAP` | `60` | Word overlap between chunks |
| `TOP_K_RETRIEVE` | `10` | Chunks retrieved |
| `TOP_K_USE` | `4` | Chunks sent to LLM |
| `CACHE_TTL_HOURS` | `168` | 7 days |
| `SYNC_INTERVAL_MINUTES` | `60` | Hourly sync |
| `DATA_DIR` | `/home/data` | `./data` for local |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose troubleshooting |

### Security: never commit secrets

Never put these values in code or git:
- `CLIENT_SECRET`
- `EMBED_API_KEY`
- `GPT_API_KEY`
- `SEARCH_API_KEY`
- `API_KEY`

Always set them in Azure portal directly. Use `.env.example` as a template that gets committed; the real `.env` is in `.gitignore`.

---

## 9. Project Folder Structure

```
veelead-helpdesk-bot/
├── .env.example              # template — committed
├── .env                      # real values — gitignored
├── .gitignore                # protects secrets
├── requirements.txt          # Python dependencies
├── README.md                 # this file
│
├── app.py                    # FastAPI server — main entry
├── config.py                 # settings loader/validator
├── scheduler.py              # hourly auto-sync (APScheduler)
├── chatbot.html              # simple frontend (reference impl)
│
├── data/                     # SQLite DBs (auto-created, gitignored)
│   ├── cache.db
│   └── sync_state.db
│
├── local_data/               # only used when SOURCE_TYPE=local
│
├── sources/                  # document source adapters
│   ├── __init__.py           # factory: picks local vs SharePoint
│   ├── base.py               # DocumentSource abstract class
│   ├── local_folder.py       # reads from ./local_data
│   └── sharepoint.py         # reads from SharePoint via Graph API
│
├── pipeline/                 # processing pipeline
│   ├── __init__.py
│   ├── extractors.py         # PDF / DOCX / HTML → text
│   ├── chunker.py            # text → chunks (table-aware)
│   ├── embedder.py           # text → vector via Azure OpenAI
│   └── classifier.py         # query → category prediction
│
└── storage/                  # persistent storage interfaces
    ├── __init__.py
    ├── search_index.py       # Azure AI Search wrapper
    └── cache.py              # SQLite cache + sync state
```

---

## 10. Local Development Setup

For developers to run the bot on their laptop before deploying.

### Prerequisites
- Python 3.11
- A SharePoint app registration's credentials (or use `local` mode for testing)
- Azure OpenAI keys
- Azure AI Search service

### Setup

```bash
# 1. Clone the repo
git clone <repo-url>
cd veelead-helpdesk-bot

# 2. Create virtual environment
python -m venv venv
# Windows:
.\venv\Scripts\Activate.ps1
# Mac/Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up config
cp .env.example .env
# Edit .env with your real values

# 5. Test config
python config.py

# 6. (For SharePoint mode) Verify connection
python test_sharepoint.py

# 7. Run the bot
uvicorn app:app --reload --port 8000

# 8. Open chatbot.html in browser
```

### Common local commands

```bash
# Check what's in the index
python -m storage.search_index stats

# View all categories
python -m storage.search_index categories

# Reset cache (forces fresh answers)
python -m storage.cache clear

# Force full re-sync
python -c "from storage.cache import reset_delta_token; reset_delta_token()"

# Then restart bot, it'll re-sync everything
```

### Run without SharePoint (offline mode)

```bash
# In .env:
SOURCE_TYPE=local
LOCAL_DATA_FOLDER=./local_data

# Drop PDFs/DOCX/HTML into ./local_data/
# Restart bot — indexes from folder instead of SharePoint
```

---

## 11. Deployment Flow (GitHub Actions)

### One-time setup

1. **Connect repo to App Service:**
   - Azure portal → App Service → Deployment Center
   - Source: GitHub
   - Authorize, pick repo + branch (`main`)
   - Azure auto-creates `.github/workflows/main_veelead-helpdesk-bot.yml`

2. **Verify workflow file** has correct paths if code is in `Helpdisk/` subfolder:

```yaml
- name: Set up Python
  uses: actions/setup-python@v5
  with:
    python-version: '3.11'

- name: Install dependencies
  working-directory: ./Helpdisk
  run: |
    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

- name: Upload artifact
  uses: actions/upload-artifact@v4
  with:
    name: python-app
    path: ./Helpdisk
```

### Normal update flow

```
Developer makes a code change
       ↓
git commit + git push origin main
       ↓
GitHub Actions runs automatically
       ↓
Builds artifact, deploys to App Service
       ↓
App Service restarts with new code (30-60 sec)
       ↓
Live at https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net
```

### Watch the deployment

- Azure portal → App Service → **Deployment Center** → see build history
- App Service → **Log stream** → live application logs
- GitHub → repo → **Actions** tab → CI/CD run history

### Rollback

If a deployment breaks something:
- Azure portal → **Deployment Center** → previous deployment → **Redeploy**

---

## 12. API Endpoints (for Frontend Developers)

Base URL: `https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net`
Auth header: `x-api-key: <key from Prethish>`

### GET /health (public)
Health check, no auth needed.

```json
{
  "status": "ok",
  "source": "SharePoint(...)",
  "index": { "document_count": 145 },
  "cache": { "entries": 27, "total_hits": 142 },
  "sync": { ... },
  "scheduler": { "running": true, "jobs": [...] }
}
```

### GET /categories (auth required)
List available categories with chunk counts.

```json
{
  "categories": [
    { "name": "IT",         "display": "IT",         "chunk_count": 45 },
    { "name": "HR",         "display": "HR",         "chunk_count": 31 },
    { "name": "Facilities", "display": "Facilities", "chunk_count": 12 }
  ]
}
```

Frontend usage: render as filter buttons at chat start.

### GET /search.json?q=...&category=... (auth required)
Main query endpoint.

**Parameters:**
- `q` (required) — user's question, URL-encoded
- `category` (optional) — restrict to one category (case-sensitive)

**Response:**
```json
{
  "answer": "To reset your M365 password, go to portal.office.com...",
  "confidence": 0.87,
  "model_used": "gpt-4o-mini",
  "cached": false,
  "numFound": 3,
  "sources": ["Acceptable Use policy.pdf"],
  "chunks": [
    {
      "text": "Password reset steps: 1. Go to portal.office.com...",
      "filename": "Acceptable Use policy.pdf",
      "pdf_url": "https://veeleaddev.sharepoint.com/.../Acceptable%20Use%20policy.pdf",
      "score": 3.8,
      "page": 4,
      "article_title": "Acceptable Use Policy",
      "category": "IT"
    }
  ],
  "suggested_followups": [
    "How do I enable MFA?",
    "What if I forgot my MFA device?",
    "How do I unlock my account?"
  ],
  "category_used": "IT",
  "category_source": "user_selected"
}
```

**Confidence interpretation:**
- `≥ 0.75` → high (green badge)
- `0.50–0.74` → moderate (yellow badge)
- `< 0.50` → low (red badge)

**category_source values:**
- `user_selected` — user clicked a category button
- `ai_predicted` — bot auto-detected from the question
- `fallback_all` — no results in selected category, searched all
- `cached` — answer came from cache

### POST /admin/reindex (auth required)
Trigger sync now.

```bash
curl -X POST -H "x-api-key: KEY" \
  https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net/admin/reindex
```

Optional: `?force_full=true` for full re-sync.

### POST /admin/reset_sync (auth required)
Clear the delta token. Next sync will be a full sync.

```bash
curl -X POST -H "x-api-key: KEY" \
  https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net/admin/reset_sync
```

### Error responses

| Code | Meaning |
|---|---|
| 401 | Missing or wrong `x-api-key` |
| 422 | Validation error (e.g. empty `q`) |
| 500 | Server error (check logs) |
| 503 | Service starting up or busy |

---

## 13. Frontend (chatbot.html)

The repo includes `chatbot.html` — a single-file reference frontend.

### What it shows
- Category filter bar at top
- Suggested questions on first load
- Chat bubbles with user questions and bot answers
- Confidence badge (color-coded)
- Category badge
- Collapsible source chunks with page numbers and "Open file" links
- Follow-up question chips

### Configuration (at top of file)

```javascript
const API_BASE = "https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net";
const API_KEY  = "veelead-secure-9f83jsdf9832@";
```

For local dev: `const API_BASE = "http://localhost:8000";`

### Hosting options for the HTML

| Option | How | Cost |
|---|---|---|
| Open file locally | Double-click chatbot.html | $0 |
| Same App Service serves it | Add static file route to FastAPI | $0 |
| Azure Static Web Apps | Deploy via GitHub | $0 (free tier) |
| Embed in intranet | iframe in SharePoint/Confluence | $0 |

**Important:** When deployed via HTTPS, the API_BASE must also be HTTPS. You cannot call HTTP from HTTPS pages (mixed content).

### Security: API key in frontend

The frontend embeds the API key, which means it's visible in browser dev tools. This is acceptable IF:
- The bot is only used on the internal company network
- The API key only allows reading documents (it does)
- The key can be rotated easily if leaked

For higher security, consider:
- Hosting the HTML behind Azure AD authentication (Easy Auth)
- Adding rate limiting on the bot
- Using a backend proxy that injects the key server-side

---

## 14. Security Model

### Defense in depth

| Layer | Protection |
|---|---|
| **Transport** | HTTPS only, TLS 1.2+ |
| **Authentication** | `x-api-key` header on every request |
| **Authorization** | SharePoint app registration is read-only |
| **Status filter** | Only `Published`/`Approved` docs indexed |
| **Secrets** | In Azure App Service env vars (encrypted), never in code |
| **Network** | Can optionally restrict to internal IPs via App Service Access Restrictions |

### Data flow security

```
1. Employee browser → HTTPS → App Service
   • TLS encrypts traffic
   • API key authenticates request

2. App Service → Azure OpenAI
   • Internal Azure network
   • Data NEVER leaves Azure boundary
   • NOT sent to OpenAI's public servers

3. App Service → SharePoint
   • Microsoft Graph API
   • OAuth 2.0 client credentials
   • Read-only scope

4. App Service → Azure AI Search
   • Internal Azure network
   • Admin key for writes, query key for reads
```

### Compliance notes
- All data stays in Microsoft Azure infrastructure
- Same compliance posture as Microsoft 365 (where SharePoint already lives)
- GDPR-compliant (data residency in your Azure region)
- No data sent to OpenAI's consumer APIs

### Incident response

If API key is leaked:
1. Generate new key
2. Update in Azure portal (App Service → Environment variables)
3. Update in frontend code
4. App Service auto-restarts
5. Old key stops working immediately

If SharePoint credentials are leaked:
1. Cynthia (admin in veeleaddev) revokes the app registration secret
2. Generate new secret
3. Update in Azure App Service env vars
4. Restart

---

## 15. Monitoring and Operations

### Daily health check (5 minutes)

Check `https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net/health`:
- `status: "ok"` ✅
- `index.document_count` matches SharePoint published count ✅
- `sync.last_sync.status: "success"` ✅
- `scheduler.running: true` ✅

### Monthly review (15 minutes)

1. **Cost check** — Azure portal → Cost Management
2. **Query volume** — `cache.total_hits` in /health
3. **Cache effectiveness** — top questions in /health (should be repetitive)
4. **Failed syncs** — log stream → search "FAILED"

### Log stream (live debugging)

Azure portal → App Service → **Log stream**

Or via CLI:
```bash
az webapp log tail --name veelead-helpdesk-bot --resource-group rg-veelead-helpdesk
```

### Key log markers

| Look for | Means |
|---|---|
| `✅ API READY` | Startup successful |
| `Indexing: <filename>` | Sync processing a file |
| `✓ N uploaded, 0 failed` | Successful indexing |
| `0 uploaded, N failed` | ❌ Problem — investigate |
| `SYNC COMPLETE +A ~U -D` | Sync finished |
| `💰 Cache HIT` | Cache working (good) |
| `LLM call failed` | Azure OpenAI issue |

### Backup considerations

- **SQLite databases** in `/home/data` — survives App Service restarts but lost if App Service is deleted. Optional: enable App Service backup feature.
- **Azure AI Search** — Microsoft-managed, no backup needed for free/basic tiers
- **SharePoint** — already backed up by your M365 plan
- **Code** — in GitHub

---

## 16. Troubleshooting

### Bot returns "I could not find that in the documents"
1. Check `/health` → `index.document_count` > 0?
2. If 0: trigger reindex via `/admin/reindex?force_full=true`
3. Check log stream for "X uploaded, 0 failed" during sync
4. If still failing: see "Date format errors" below

### Date format errors (Edm.DateTimeOffset)
Symptom: `Cannot convert the literal '2026-05-04T18:30:00+00:00Z'`
Fix: `sources/base.py` must use `_iso_for_search()` helper, not raw `isoformat() + "Z"`

### SharePoint metadata not loading
Symptom: `Could not fetch metadata for <filename>: Field 'FileLeafRef' cannot be referenced...`
Fix: `sources/sharepoint.py` must use cached bulk-fetch, not per-file filter

### Search index errors after schema update
Symptom: `Could not find a property named 'X'`
Fix:
```bash
python -m storage.search_index drop
python -c "from storage.cache import reset_delta_token; reset_delta_token()"
# Restart bot — it recreates index with new schema and re-syncs
```

### Cache returning wrong/stale answers
```bash
python -m storage.cache clear
```

### App Service won't start after deployment
1. Check Log stream for the actual error
2. Most common: wrong startup command, missing env var, package not in requirements.txt
3. Rollback to previous deployment via Deployment Center

### High costs
1. Check `/health` → `cache.entries` and `cache.total_hits`
2. If cache hits low: increase `CACHE_TTL_HOURS`
3. Check if `gpt-4o` is being used too often (look at logs for `model_used`)
4. Consider rate limiting if abuse suspected

---

## 17. Capabilities and Limits

### What the bot CAN do
✅ Answer questions from any indexed published document
✅ Cite source document and page number
✅ Filter by category
✅ Suggest follow-up questions
✅ Detect SharePoint changes within 1 hour
✅ Handle PDF, DOCX, and HTML files
✅ Preserve tables in document chunks
✅ Cache repeat questions for instant responses
✅ Route complex queries to a smarter model
✅ Gracefully fall back when no results match a category

### What the bot CANNOT do (yet)
❌ Answer from documents not marked Published
❌ Search images or scanned PDFs (no OCR)
❌ Multi-turn conversation memory (each query is independent)
❌ Personalize answers per user
❌ Write back to SharePoint (e.g. increment view counts)
❌ Handle non-English documents (English-tuned)
❌ Answer questions about today's date/weather (no real-time data)

### Performance characteristics
- **Cold start (after restart)**: ~5 seconds
- **Cached query**: ~10 ms
- **Fresh query**: 1–3 seconds
- **Complex query (`gpt-4o`)**: 3–6 seconds
- **Full re-sync**: ~30 sec for 14 docs, ~5 min for 200 docs

### Scalability limits

| Resource | Limit | When you hit it |
|---|---|---|
| Azure AI Search Free tier | 50 MB / 10,000 docs | ~300 documents |
| App Service B1 | 1 vCPU, ~50 concurrent users | Heavy lunch-time load |
| Azure OpenAI rate limits | ~30 req/min (default) | Spike of >30 simultaneous users |

### Upgrade paths
- More docs → Azure AI Search **Basic** ($25/mo)
- More users → App Service **B2** ($26/mo) or **S1** ($69/mo)
- More queries → Request Azure OpenAI quota increase (free)

---

## Quick Reference Card

```
URL:           https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net
API key:       see Azure App Service → Environment variables
Source:        SharePoint Helpdesk (veeleaddev tenant)
Sync:          Every 60 minutes
Cache TTL:     7 days
AI Search:     Free tier ($0/mo, ~300 docs max)
Hosting:       App Service B1 ($13/mo)
OpenAI usage:  ~$5-10/mo at current volume
Total cost:    ~$18/mo at launch, ~$50/mo at 1000 docs
Status check:  GET /health
Manual sync:   POST /admin/reindex
Log stream:    Azure portal → App Service → Log stream
```

---

## Contact

- **Owner**: Prethish G (`prethish.g@veelead.com`)
- **SharePoint admin** (veeleaddev tenant): Cynthia (`Cynthia@veeleaddev.onmicrosoft.com`)
- **For SharePoint permission issues**: contact Raj
- **For Azure / OpenAI issues**: contact Prethish

---

*Last updated: May 2026*
