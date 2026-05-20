# Veelead Helpdesk Bot Deployment README

## Overview

This document explains how the **veelead-helpdesk-bot** backend is hosted on Azure App Service, what configuration is required, how deployments work, what environment variables are used, and what ongoing monthly costs to expect for App Service, Azure AI Search, and Azure OpenAI. The Azure portal for this app shows the Web App named **veelead-helpdesk-bot** with configuration areas including **Environment variables**, **Configuration**, **Deployment Center**, and **Log stream**.[cite:52]

The application is hosted as an Azure **Web App** and uses a Python runtime on App Service. The hosting pattern discussed for this project uses a Linux App Service with Python 3.11 and a startup command for a FastAPI backend running through Gunicorn with Uvicorn workers.[cite:36][cite:54]

## Hosted URL

The deployed backend is intended to run at:

- `https://veelead-helpdesk-bot.azurewebsites.net`

Example endpoints:
- `https://veelead-helpdesk-bot.azurewebsites.net/health`
- `https://veelead-helpdesk-bot.azurewebsites.net/search.json?q=hello`

These endpoint patterns match the Azure Web App name and the health/search routes used during setup and testing discussions for this deployment.[cite:52]

## Azure Hosting Setup

The backend is deployed on **Azure App Service** under the Web App name **veelead-helpdesk-bot**. In the Azure portal, the app is managed from the App Service resource page where settings such as environment variables, deployment center, startup configuration, and logs are available.[cite:52]

The recommended hosting plan for this bot is **Azure App Service Linux Basic B1**. Microsoft’s App Service for Linux pricing page lists B1 as a 1-core plan with 1.75 GB RAM and 10 GB storage, and shows a starting monthly cost around **$13.14/month** on the referenced pricing page.[cite:54]

## Runtime and Startup Command

The backend is expected to run as a **FastAPI** application. Microsoft’s Python web app quickstart for Azure App Service shows FastAPI deployments using Gunicorn with the Uvicorn worker class, which matches the command used for this bot.[cite:36]

Configured startup command:

```bash
gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 app:app
```

If the FastAPI object is located in a different file, the import target must be changed accordingly. For example:

- `app:app` means `app.py` contains `app = FastAPI()`
- `main:app` means `main.py` contains `app = FastAPI()`[cite:36]

## Source Code and Deployment Flow

The backend code is stored in a GitHub repository and is deployed through **GitHub Actions** connected from Azure **Deployment Center**. Microsoft documents GitHub Actions as a supported and common deployment flow for Python applications on Azure App Service.[cite:3][cite:4]

In this project, the backend code is stored inside the `Helpdisk/` subfolder rather than the repository root. Because of that, the GitHub Actions workflow must install dependencies from `Helpdisk/requirements.txt` and deploy the `Helpdisk/` folder as the package artifact instead of uploading the whole repository root.[cite:4][cite:30]

### GitHub Actions notes

The workflow should:
- Set Python version to 3.11.[cite:3]
- Run dependency installation from the `Helpdisk/` folder.[cite:4]
- Upload `./Helpdisk` as the deployment package.[cite:4][cite:30]
- Deploy to the Azure Web App named `veelead-helpdesk-bot`.[cite:4]

## Environment Variables

The Azure portal page for this app includes **Settings → Environment variables**, which is the correct place to store runtime configuration and secrets for the backend.[cite:52]

The following environment variables were identified for the RAG bot configuration:

| Name | Purpose |
|---|---|
| `SOURCE_TYPE` | Selects the content source mode, such as SharePoint. |
| `TENANT_ID` | Microsoft Entra tenant ID used for authentication. |
| `CLIENT_ID` | App registration client ID for SharePoint/Graph access. |
| `CLIENT_SECRET` | Secret used by the app registration for authentication. |
| `SHAREPOINT_SITE_URL` | SharePoint site URL used as the knowledge source. |
| `SHAREPOINT_LIBRARY` | SharePoint document library name. |
| `EMBED_ENDPOINT` | Azure OpenAI endpoint for embeddings. |
| `EMBED_API_KEY` | Key for the embeddings endpoint. |
| `EMBED_DEPLOYMENT` | Embedding model deployment name. |
| `EMBED_API_VER` | API version used for embeddings. |
| `GPT_ENDPOINT` | Azure OpenAI endpoint for chat/completions. |
| `GPT_API_KEY` | Key for the chat endpoint. |
| `GPT_MINI_DEPLOY` | Smaller Azure OpenAI deployment name, such as `gpt-4o-mini`. |
| `GPT_LARGE_DEPLOY` | Larger Azure OpenAI deployment name, such as `gpt-4o`. |
| `GPT_API_VER` | API version used for GPT calls. |
| `SEARCH_ENDPOINT` | Azure AI Search service endpoint. |
| `SEARCH_API_KEY` | Azure AI Search admin or query key used by the app. |
| `SEARCH_INDEX` | Azure AI Search index name. |
| `API_KEY` | App-level API key expected by the backend for protected requests. |
| `CHUNK_SIZE` | Chunk size used while splitting source documents. |
| `CHUNK_OVERLAP` | Overlap between chunks for retrieval indexing. |
| `TOP_K_RETRIEVE` | Number of retrieval candidates fetched from search. |
| `TOP_K_USE` | Number of top retrieved chunks used in final prompting. |
| `CACHE_TTL_HOURS` | Cache lifetime in hours. |
| `SYNC_INTERVAL_MINUTES` | Sync interval for periodic content refresh. |
| `DATA_DIR` | Local runtime data directory path, often `/tmp/data`. |
| `LOG_LEVEL` | Logging verbosity, such as `INFO`. |

These settings are added in the Azure App Service **Environment variables** page so the application can read them at runtime instead of hardcoding secrets in source files.[cite:52][cite:28]

### Security note

Secrets such as `CLIENT_SECRET`, `EMBED_API_KEY`, `GPT_API_KEY`, `SEARCH_API_KEY`, and `API_KEY` should be treated as sensitive credentials and must never be committed to GitHub. Azure App Service supports storing these values as application settings exposed to the app as environment variables, which is the correct pattern for production deployments.[cite:28][cite:14]

## Required Python Packages

The backend requires FastAPI and its production server dependencies in `Helpdisk/requirements.txt`. For FastAPI on Azure App Service, packages such as `gunicorn`, `fastapi`, `uvicorn[standard]`, and `pydantic` are necessary for the app to start correctly.[cite:21][cite:36]

The discussed dependency set also includes:
- `python-dotenv` for configuration loading.
- `openai` for Azure OpenAI API access.
- `azure-search-documents` and `azure-core` for Azure AI Search.
- `msal` and `requests` for Microsoft Graph and SharePoint integration.
- `pypdf`, `python-docx`, `beautifulsoup4`, and `lxml` for document parsing.
- `apscheduler` for periodic sync jobs.[cite:21][cite:36]

## Monthly Cost Estimate

### App Service cost

The Azure App Service Linux pricing page shows the **Basic B1** plan at about **$13.14/month**, with 1 core, 1.75 GB RAM, and 10 GB storage on the referenced pricing page. This amount covers the App Service hosting plan itself and does not include separate costs for Azure AI Search, Azure OpenAI usage, networking, or other Azure services.[cite:54][cite:61]

### Azure AI Search cost

Azure AI Search pricing depends on the selected tier and number of search units. Microsoft’s pricing page shows that the service has tiers including **Free**, **Basic**, and **Standard** families, with Basic offering 15 GB storage and up to 15 indexes, while Standard S1 offers 160 GB storage and broader scaling options.[cite:59][cite:65]

The exact monthly price depends on the tier, replicas, and partitions selected in the search service. Azure AI Search billing is driven mainly by provisioned capacity, and storage/query needs increase cost as scale grows.[cite:59][cite:68]

For a small internal RAG bot, common cost drivers are:
- Search tier selected, such as Basic or Standard S1.[cite:59]
- Number of replicas for query availability and performance.[cite:59]
- Number of partitions for index size and throughput.[cite:59][cite:68]
- Optional AI enrichment features during ingestion, if enabled.[cite:68]

### Azure OpenAI model cost

Azure OpenAI cost depends on the specific model deployments and the total number of input/output tokens processed. In this bot, the configured models discussed include **gpt-4o**, **gpt-4o-mini**, and **text-embedding-3-small**, so cost is usage-based rather than a fixed monthly fee.[cite:63][cite:60]

In practical terms:
- **`text-embedding-3-small`** is used when documents are embedded for vector search, and the cost grows with the amount of source text indexed.[cite:63]
- **`gpt-4o-mini`** is typically used as a lower-cost response model for routine chat or retrieval-augmented answers.[cite:63][cite:60]
- **`gpt-4o`** is a higher-capability model and generally costs more than the mini model, so using it for all requests increases monthly spend faster.[cite:63]

A simple cost rule for this bot is:
- More document ingestion increases **embedding** cost.
- More user queries increase **chat model** cost.
- Longer prompts and larger retrieved context increase token usage and therefore cost.[cite:63][cite:68]

Because Azure OpenAI pricing varies by model and usage volume, the most accurate estimate should be calculated from expected monthly document ingestion volume and expected monthly user query count, then checked against the Azure pricing calculator or service pricing pages.[cite:63]

## What happens when code is updated

When developers update the code and push changes to the configured GitHub branch, the GitHub Actions workflow runs automatically and redeploys the application to Azure App Service. Microsoft documents this branch-based CI/CD pattern for Python web apps deployed from GitHub Actions.[cite:3][cite:4]

The normal update flow is:
1. Developer edits backend code in the GitHub repository.
2. Developer commits and pushes changes to `main`.
3. GitHub Actions starts the build workflow.[cite:3][cite:4]
4. Dependencies are installed from `Helpdisk/requirements.txt`.[cite:4]
5. The `Helpdisk/` package is uploaded and deployed to `veelead-helpdesk-bot`.[cite:4][cite:30]
6. Azure App Service restarts the app with the updated code.[cite:14]

If only environment variables are changed in the Azure portal, the code repository does not change, but Azure App Service still restarts the application after settings are saved so the new values are available to the running app.[cite:14][cite:28]

## How to update the code safely

Recommended deployment process:

1. Update code in the repository.
2. If new packages are used, update `Helpdisk/requirements.txt` at the same time.
3. If new config values are needed, add them in Azure **Environment variables** before or immediately after deployment.[cite:52][cite:28]
4. Push to `main` to trigger GitHub Actions.[cite:3][cite:4]
5. Watch **Deployment Center** and **Log stream** in Azure to confirm successful startup and check for runtime errors.[cite:52]
6. Test the live endpoints such as `/health` and `/search.json` after deployment.[cite:52]

## Operational notes

Key Azure portal areas for this app include:
- **Environment variables** for API keys and configuration.[cite:52]
- **Configuration** for startup command and general runtime settings.[cite:52]
- **Deployment Center** for CI/CD integration and deployment history.[cite:52]
- **Log stream** for startup logs and runtime troubleshooting.[cite:52]

If the application fails after deployment, the most common causes are:
- Wrong startup command module path, such as `app:app` when the file is actually named differently.[cite:36]
- Missing packages in `requirements.txt`.[cite:21][cite:36]
- Missing or incorrect environment variables in Azure.[cite:28]
- GitHub Actions deploying the wrong folder when the backend is stored in `Helpdisk/`.[cite:4][cite:30]

## Recommended improvements

For better long-term maintainability and security, consider the following:
- Rotate any secrets that were shared outside secure systems, then store only the rotated versions in Azure App Service settings.[cite:14][cite:28]
- Consider using Azure Key Vault for production-grade secret management if the project grows.[cite:14]
- Track monthly App Service, Azure AI Search, and Azure OpenAI usage separately so cost increases can be attributed to hosting, retrieval, or model traffic.[cite:54][cite:59][cite:63]
- Use `gpt-4o-mini` for most routine chatbot responses and reserve `gpt-4o` for cases that genuinely need higher reasoning quality, because model choice is a major cost lever.[cite:63]
